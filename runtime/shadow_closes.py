"""Frozen nonfinancial shadow observations, durable admission and replay.

No orders, model activation, worker or disk creation on import. Durability starts
after atomic outbox publication, not at price lookup or on hardware power loss.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping
from uuid import uuid4

from utils.atomic_json import read_json_strict, write_json_atomic
from utils.file_ownership import exclusive_file_guard

VERSION = 1
MAX_BYTES = 1024 * 1024


def _freeze(value: Any, depth=0):
    if depth > 16:
        raise ValueError("Shadow snapshot nesting exceeds limit")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dt.datetime):
        return value.isoformat() if str(value) != "NaT" else None
    if isinstance(value, Mapping):
        return {str(key): _freeze(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_freeze(item, depth + 1) for item in value]
    # Native NumPy scalars and pandas missingness, never arbitrary repr coercion.
    import numpy as np
    import pandas as pd
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        return _freeze(value.item(), depth + 1)
    raise TypeError("Unsupported shadow snapshot value")


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _time(value):
    parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def freeze_observation(address, vector, *, closed_at, close, context, instance_id=None):
    if not str(address).strip():
        raise ValueError('Missing shadow address')
    features = vector.to_dict() if hasattr(vector, 'to_dict') else dict(vector or {})
    from features.auxiliary_semantics import PROOF_COLUMN
    from features.token_clock_semantics import PROOF_COLUMN as CLOCK
    from features.valuation_semantics import PROOF_COLUMN as VALUATION
    for key in (PROOF_COLUMN, CLOCK, VALUATION):
        if key in getattr(vector, 'attrs', {}):
            features[key] = vector.attrs[key]
    from utils.runtime_context import runtime_context_payload
    context = {**runtime_context_payload(), **context}
    observation = _freeze({'version': VERSION, 'id': 'shadow_' + uuid4().hex,
        'address': str(address), 'closed_at': closed_at,
        'has_features': vector is not None, 'features': features,
        'close': close, 'context': context, 'instance_id': instance_id})
    observation['closed_at'] = _time(observation['closed_at']).isoformat()
    if observation['close'].get('pnl_pct') is None:
        observation['close']['label'] = None
    if len(_encoded(observation)) > MAX_BYTES:
        raise ValueError('Shadow observation exceeds size limit')
    return observation


def _validate(observation):
    if (not isinstance(observation, dict) or observation.get('version') != VERSION
            or not re.fullmatch(r'shadow_[0-9a-f]{32}', str(observation.get('id')))
            or not isinstance(observation.get('features'), dict)
            or not isinstance(observation.get('close'), dict)
            or not isinstance(observation.get('context'), dict)
            or not isinstance(observation.get('has_features'), bool)
            or not str(observation.get('address') or '').strip()):
        raise ValueError('Invalid shadow observation')
    _time(observation['closed_at'])
    close = observation['close']
    if (set(close) != {'regime', 'pnl_pct', 'label', 'exit_reason', 'close_price_usd', 'shadow_kind', 'extra'}
            or not isinstance(close['extra'], dict)
            or close['label'] not in (None, 0, 1)
            or (close['pnl_pct'] is None and close['label'] is not None)):
        raise ValueError('Invalid shadow close targets')
    data = _encoded(observation)
    if len(data) > MAX_BYTES:
        raise ValueError('Shadow observation exceeds size limit')
    return hashlib.sha256(data).hexdigest()


def persist_observation(root: Path, observation: dict, *, feature_writer=None, journal_writer=None):
    observation = json.loads(_encoded(observation))
    digest = _validate(observation)
    base = Path(root) / 'data/research/shadow_close_outbox'
    pending = base / 'pending' / (observation['id'] + '.json')
    complete = base / 'complete' / pending.name
    guard = base / (observation['id'] + '.guard')
    with exclusive_file_guard(guard, deadline=time.monotonic() + 30., poll_seconds=.05):
        prior = complete if complete.exists() else pending
        if prior.exists():
            if prior.stat().st_size > MAX_BYTES + 4096:
                raise ValueError('Oversized shadow outbox record')
            saved = read_json_strict(prior)
            if saved != {'observation': observation, 'sha256': digest}:
                raise ValueError('Conflicting frozen shadow close')
            if prior == complete:
                return {'complete': True, 'replayed': True}
        else:
            write_json_atomic(pending, {'observation': observation, 'sha256': digest})
        if feature_writer is None:
            from features.store import append as feature_writer
        if journal_writer is None:
            from analytics.research_runtime import record_shadow_close as journal_writer
        close = observation['close']
        clock = _time(observation['closed_at'])
        if observation['has_features']:
            vector = dict(observation['features'])
            if vector.get('timestamp') is not None:
                vector['timestamp'] = _time(vector['timestamp'])
            result = feature_writer(vector, close['label'], target_total_pnl_pct=close['pnl_pct'],
                sample_type='shadow_close', strict=True, partition_at=clock,
                outcome_targets={'max_pnl_pct_seen': close['extra'].get('max_pnl_pct_seen'),
                    'outcome_closed_at': clock, 'outcome_observation_id': observation['id'],
                    'outcome_observation_sha256': digest})
            if result is not True:
                raise OSError('Shadow feature persistence is unconfirmed')
        result = journal_writer(observation['address'], **close,
            observation_id=observation['id'], observation_sha256=digest, observed_at=clock,
            context_snapshot=observation['context'], shadow_instance_id=observation['instance_id'])
        if result is False:
            raise OSError('Shadow research persistence is unconfirmed')
        complete.parent.mkdir(parents=True, exist_ok=True)
        os.replace(pending, complete)
        return {'complete': True, 'replayed': False}


def replay_pending(root: Path, *, max_items=32):
    if type(max_items) is not int or not 1 <= max_items <= 256:
        raise ValueError('Invalid shadow replay budget')
    path = Path(root) / 'data/research/shadow_close_outbox/pending'
    result = {'complete': 0, 'failed': 0}
    for file in sorted(path.glob('shadow_*.json'))[:max_items]:
        try:
            if file.stat().st_size > MAX_BYTES + 4096:
                raise ValueError('Oversized shadow outbox record')
            saved = read_json_strict(file)
            if hashlib.sha256(_encoded(saved['observation'])).hexdigest() != saved['sha256']:
                raise ValueError('Corrupt shadow outbox checksum')
            if file.name != saved['observation']['id'] + '.json':
                raise ValueError('Shadow outbox identity mismatch')
            persist_observation(root, saved['observation'])
            result['complete'] += 1
        except Exception as exc:
            result['failed'] += 1
            logging.getLogger(__name__).warning('Shadow recovery remains pending %s (%s)',
                file.stem, type(exc).__name__)
    return result
