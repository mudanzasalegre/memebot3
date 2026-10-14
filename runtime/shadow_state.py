"""Owned durable nonfinancial shadow state; no IO or workers on import.

Only prospectively saved snapshots can be restored. The legacy portfolio is
not a feature receipt and is never backfilled into one. Atomic replacement is
process-crash protection, not a hardware power-loss or financial guarantee.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import time

from runtime.shadow_closes import _encoded, _freeze, _time, _validate as close_digest, MAX_BYTES as CLOSE_MAX_BYTES
from utils.atomic_json import read_json_strict, write_json_atomic
from utils.file_ownership import exclusive_file_guard

VERSION = 1
MAX_BYTES = 2 * CLOSE_MAX_BYTES + 4096
INTERNAL = '_shadow_state'
IMMUTABLE = ('vec', 'opened_at', 'buy_price_usd', 'entry_regime', 'runner_exit_profile',
    'reason', 'stage', 'ml_proba', 'threshold', 'rank_info', 'shadow_kind', 'price_source', 'buy_liquidity_usd')


def snapshot_vector(vector):
    """Detach values and original attrs before an async price lookup yields."""
    if vector is None: return None
    from features.auxiliary_semantics import PROOF_COLUMN
    from features.token_clock_semantics import PROOF_COLUMN as CLOCK
    from features.valuation_semantics import PROOF_COLUMN as VALUATION
    values = vector.to_dict() if hasattr(vector, 'to_dict') else dict(vector)
    for key in (PROOF_COLUMN, CLOCK, VALUATION):
        if key in getattr(vector, 'attrs', {}): values[key] = vector.attrs[key]
    snapshot = _freeze(values)
    if len(_encoded(snapshot)) > CLOSE_MAX_BYTES:
        raise ValueError('Shadow vector exceeds size limit')
    return snapshot


def _limit(record):
    # Admission stays at the original bound. A closing checkpoint also carries
    # the independently bounded frozen close; account for both, not just one.
    return MAX_BYTES if 'close_observation' in record.get('state', {}) else CLOSE_MAX_BYTES


def _paths(root, address):
    if not isinstance(address, str) or not address.strip() or len(address) > 4096:
        raise ValueError('Invalid shadow state address')
    base = Path(root) / 'data/research/shadow_state'
    key = hashlib.sha256(address.encode()).hexdigest()
    return base, base / 'active' / (key + '.json'), base / (key + '.guard')


def _logical(record):
    return {key: value for key, value in record.items() if key not in {'published', 'generation'}}


def _validate(envelope):
    if not isinstance(envelope, dict) or set(envelope) != {'record', 'sha256'}:
        raise ValueError('Invalid shadow state envelope')
    record = envelope['record']
    if (not isinstance(record, dict) or set(record) != {'version', 'address', 'instance_id',
            'generation', 'state', 'context', 'opening', 'partial', 'published'}
            or type(record.get('version')) is not int or record.get('version') != VERSION
            or not re.fullmatch('[0-9a-f]{32}', str(record.get('instance_id')))
            or type(record.get('generation')) is not int or record['generation'] < 0
            or not isinstance(record.get('state'), dict) or not isinstance(record.get('context'), dict)
            or set(record.get('published', {})) != {'opening', 'partial'}
            or any(type(value) is not bool for value in record['published'].values())):
        raise ValueError('Invalid shadow state record')
    state = record['state']
    if state.get('shadow_instance_id') != record['instance_id'] or not isinstance(state.get('vec'), (dict, type(None))):
        raise ValueError('Invalid original shadow state identity')
    _time(state['opened_at'])
    _paths(Path('.'), record['address'])
    partial = record.get('partial')
    if partial is not None:
        if set(partial) != {'id', 'observed_at', 'pnl_pct', 'fraction_sold'}:
            raise ValueError('Invalid shadow partial fields')
        if (type(partial['pnl_pct']) not in (int, float) or not math.isfinite(partial['pnl_pct'])
                or type(partial['fraction_sold']) not in (int, float)
                or not math.isfinite(partial['fraction_sold']) or not 0 < partial['fraction_sold'] < 1
                or state.get('partial_taken') is not True
                or not math.isclose(state.get('remaining_fraction', -1), 1 - partial['fraction_sold'], abs_tol=1e-12)
                or not math.isclose(state.get('realized_pnl_ratio', float('nan')),
                    partial['fraction_sold'] * partial['pnl_pct'] / 100., rel_tol=1e-12, abs_tol=1e-12)):
            raise ValueError('Shadow partial prefix is inconsistent')
    elif record.get('opening') is not None and state.get('partial_taken') is True:
        raise ValueError('Original shadow partial event is absent')
    if 'close_observation' in state:
        close_digest(state['close_observation'])
        if (state['close_observation']['address'] != record['address']
                or state['close_observation']['instance_id'] != record['instance_id']):
            raise ValueError('Shadow close belongs to another state')
    for kind in ('opening', 'partial'):
        event = record.get(kind)
        if event is not None:
            if not isinstance(event, dict): raise ValueError('Invalid shadow state event')
            if kind == 'opening' and (set(event) != {'id', 'observed_at', 'payload', 'shadow_kind'}
                    or not isinstance(event['payload'], dict) or not isinstance(event['shadow_kind'], str)):
                raise ValueError('Invalid original shadow opening')
            _time(event['observed_at'])
            if event.get('id') != f"shadow_state_{record['instance_id']}_{kind}":
                raise ValueError('Invalid shadow state event identity')
    data = _encoded(record)
    if len(data) > _limit(record) or hashlib.sha256(data).hexdigest() != envelope['sha256']:
        raise ValueError('Shadow state checksum or size mismatch')
    return record


def _envelope(record):
    data = _encoded(record)
    if len(data) > _limit(record): raise ValueError('Shadow state exceeds size limit')
    envelope = {'record': record, 'sha256': hashlib.sha256(data).hexdigest()}
    _validate(envelope)
    return envelope


def _read(path):
    if path.stat().st_size > MAX_BYTES + 4096: raise ValueError('Oversized shadow state')
    envelope = read_json_strict(path)
    _validate(envelope)
    return envelope


def checkpoint(root, address, state, *, context=None, opening=None, partial=None):
    """Save before publishing effects; original entry and generation cannot drift."""
    previous = state.get(INTERNAL)
    if previous is not None: _validate(previous)
    vector = state.get('vec')
    values = {key: value for key, value in state.items() if key not in
        {INTERNAL, 'opening_pending', '_shadow_pending_partial'}}
    closing = values.pop('close_observation', None)
    if vector is not None:
        values['vec'] = snapshot_vector(vector)
    snapshot = _freeze(values)
    if closing is not None:
        # The close is already a bounded, frozen JSON observation. Preserve its
        # original nesting budget instead of imposing extra envelope depth.
        close_digest(closing)
        snapshot['close_observation'] = json.loads(_encoded(closing))
    instance = snapshot['shadow_instance_id']
    old = previous['record'] if previous else None
    record = {'version': VERSION, 'address': address, 'instance_id': instance,
        'generation': old['generation'] if old else 0, 'state': snapshot,
        'context': deepcopy(old['context']) if old else _freeze(context or {}),
        'opening': deepcopy(old.get('opening')) if old else _freeze(opening),
        'partial': deepcopy(old.get('partial')) if old else None,
        'published': deepcopy(old['published']) if old else {'opening': False, 'partial': False}}
    if partial is None:
        partial = state.get('_shadow_pending_partial')
    if partial is not None:
        event = _freeze(partial)
        if record['partial'] is not None and record['partial'] != event:
            raise ValueError('A shadow partial cannot be replaced')
        if record['partial'] is None:
            record['partial'] = event
            record['published']['partial'] = False
    incoming = _envelope(record)
    base, active, guard = _paths(root, address)
    with exclusive_file_guard(guard, deadline=time.monotonic() + 30., poll_seconds=.05):
        if (base / 'complete' / (instance + '.json')).exists():
            raise ValueError('Completed shadow state cannot reopen')
        if active.exists():
            saved = _read(active)
            prior = saved['record']
            if prior['instance_id'] != instance:
                raise ValueError('Shadow address belongs to another instance')
            if (prior['context'] != record['context'] or prior.get('opening') != record.get('opening')
                    or any(prior['state'].get(key) != snapshot.get(key) for key in IMMUTABLE)):
                raise ValueError('Original shadow entry changed')
            if (prior['state'].get('close_observation') is not None
                    and prior['state']['close_observation'] != snapshot.get('close_observation')):
                raise ValueError('Original shadow close changed')
            if _logical(prior) == _logical(record): return saved
            if old is None or old['generation'] != prior['generation']:
                raise ValueError('Stale shadow state generation')
            record['generation'] = prior['generation'] + 1
            record['published']['opening'] = prior['published']['opening']
            if prior.get('partial') == record.get('partial'):
                record['published']['partial'] = prior['published']['partial']
            incoming = _envelope(record)
        write_json_atomic(active, incoming)
        return incoming


def publish_pending(root, envelope, *, research=None):
    """Replay only saved original event IDs/times; acknowledge confirmed IO."""
    expected = _validate(envelope)
    _, active, guard = _paths(root, expected['address'])
    if research is None:
        from analytics import research_runtime as research
    with exclusive_file_guard(guard, deadline=time.monotonic() + 30., poll_seconds=.05):
        current = _read(active)
        record = current['record']
        if record['instance_id'] != expected['instance_id']:
            raise ValueError('Shadow state publication owner changed')
        for kind in ('opening', 'partial'):
            event = record.get(kind)
            if event is None or record['published'][kind]: continue
            options = dict(observed_at=_time(event['observed_at']),
                shadow_state_observation_id=event['id'], shadow_instance_id=record['instance_id'])
            if kind == 'opening':
                result = research.record_shadow_open(record['address'], payload=event['payload'],
                    shadow_kind=event['shadow_kind'], **options)
            else:
                result = research.record_shadow_partial(record['address'], pnl_pct=event['pnl_pct'],
                    fraction_sold=event['fraction_sold'], context_snapshot=record['context'], **options)
            if result is not True:
                raise OSError('Shadow state event persistence is unconfirmed')
            record['published'][kind] = True
            current = _envelope(record)
            write_json_atomic(active, current)
        research.restore_shadow_context(record['address'], context={**record['context'],
            **(record.get('opening') or {}).get('payload', {}),
            'shadow_instance_id': record['instance_id'], 'partial_taken': bool(record['state'].get('partial_taken'))})
        return current


def restore(root, *, exclude_addresses=(), max_items=32, after=None, research=None):
    if type(max_items) is not int or not 1 <= max_items <= 256:
        raise ValueError('Invalid shadow restore budget')
    base = Path(root) / 'data/research/shadow_state/active'
    excluded = {hashlib.sha256(address.encode()).hexdigest() + '.json' for address in exclude_addresses}
    files = sorted(file for file in base.glob('*.json') if file.name not in excluded)
    if after is not None:
        files = [file for file in files if file.name > after] + [file for file in files if file.name <= after]
    result = {'positions': {}, 'failed': [], 'cursor': after}
    for file in files[:max_items]:
        result['cursor'] = file.name
        try:
            saved = _read(file)
            record = saved['record']
            if file != _paths(root, record['address'])[1]: raise ValueError('Shadow state path identity mismatch')
            saved = publish_pending(root, saved, research=research)
            state = deepcopy(saved['record']['state'])
            state['opened_at'] = _time(state['opened_at'])
            state[INTERNAL] = saved
            result['positions'][record['address']] = state
        except Exception as exc:
            result['failed'].append({'file': file.name, 'error': type(exc).__name__})
            logging.getLogger(__name__).warning('Shadow state remains pending %s (%s)', file.stem, type(exc).__name__)
    return result


def retire(root, envelope, *, research=None):
    """Archive only after the identical native close outbox is complete."""
    record = _validate(envelope)
    base, active, guard = _paths(root, record['address'])
    complete = base / 'complete' / (record['instance_id'] + '.json')
    observation = record['state'].get('close_observation')
    if observation is None: raise ValueError('Cannot retire an open shadow')
    digest = close_digest(observation)
    outbox = Path(root) / 'data/research/shadow_close_outbox/complete' / (observation['id'] + '.json')
    with exclusive_file_guard(guard, deadline=time.monotonic() + 30., poll_seconds=.05):
        if complete.exists():
            if _read(complete) != envelope: raise ValueError('Conflicting retired shadow state')
            if research is not None:
                research.finish_shadow_context(record['address'], shadow_instance_id=record['instance_id'])
            return True
        if (not outbox.exists() or outbox.stat().st_size > CLOSE_MAX_BYTES + 4096
                or read_json_strict(outbox) != {'observation': observation, 'sha256': digest}):
            raise OSError('Shadow close is not durably complete')
        if _read(active) != envelope: raise ValueError('Shadow retirement owner changed')
        complete.parent.mkdir(parents=True, exist_ok=True)
        import os
        os.replace(active, complete)
        if research is not None:
            research.finish_shadow_context(record['address'], shadow_instance_id=record['instance_id'])
        return True
