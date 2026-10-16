"""Read-only PAPER cash prerequisite, never a trading authorization.

A cached report may locate a cohort but cannot approve it. New manual profiles
freeze the original identity and selected-cash digest; startup re-reads raw data.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any

from analytics.current_run import parse_time
from analytics.forward_evidence import collect_forward_evidence, forward_acceptance


BINDING_ENV_FIELDS = {
    'proposal_id': 'LIVE_PAPER_PROPOSAL_ID',
    'run_id': 'LIVE_PAPER_EVIDENCE_RUN_ID',
    'started_at': 'LIVE_PAPER_EVIDENCE_STARTED_AT_UTC',
    'config_hash': 'LIVE_PAPER_EVIDENCE_CONFIG_HASH',
    'profile': 'LIVE_PAPER_EVIDENCE_PROFILE',
    'cash_sha256': 'LIVE_PAPER_EVIDENCE_SHA256',
}


def binding_from_profile(values: dict[str, Any]) -> dict[str, Any]:
    return {key: values.get(name) for key, name in BINDING_ENV_FIELDS.items()}


def binding_profile_values(binding: dict[str, Any]) -> dict[str, str]:
    return {name: str(binding[key]) for key, name in BINDING_ENV_FIELDS.items()}


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256 and not any(ord(c) < 32 for c in value)


def inspect_paper_evidence(root: Path, *, binding: dict[str, Any] | None = None) -> dict[str, Any]:
    errors = []
    expected = binding is not None
    if binding is None:
        try:
            report = json.loads((root/'data/metrics/paper_forward_report.json').read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError):
            report = {}
        report = report if isinstance(report, dict) else {}
        locator = report.get('forward_evidence')
        locator = locator if isinstance(locator, dict) else {}
        binding = {'proposal_id': report.get('proposal_id'), 'run_id': locator.get('run_id'),
                   'started_at': locator.get('window_started_at'), 'config_hash': locator.get('config_hash'),
                   'profile': locator.get('profile')}
    binding = dict(binding) if isinstance(binding, dict) else {}
    for field in ('proposal_id', 'run_id', 'started_at', 'config_hash', 'profile'):
        if not _text(binding.get(field)):
            errors.append('paper_identity_missing_or_invalid:'+field)
    if str(binding.get('run_id') or '').strip().lower() in {'legacy', 'smoke'}:
        errors.append('paper_identity_not_prospective')
    if parse_time(binding.get('started_at')) is None:
        errors.append('paper_start_invalid')
    if expected and (not isinstance(binding.get('cash_sha256'), str)
                     or re.fullmatch('[0-9a-f]{64}', binding['cash_sha256']) is None):
        errors.append('paper_cash_binding_missing_or_invalid')
    evidence = {}
    if not errors:
        try:
            evidence = collect_forward_evidence(root, run_id=binding['run_id'],
                started_at=binding['started_at'], config_hash=binding['config_hash'], profile=binding['profile'])
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            errors.append('paper_original_evidence_unreadable')
    acceptance = forward_acceptance(evidence)
    errors.extend(acceptance['rejection_reasons'])
    digest = evidence.get('selected_cash_evidence_sha256')
    if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
        errors.append('paper_original_cash_digest_missing')
    elif expected and digest != binding.get('cash_sha256'):
        errors.append('paper_original_cash_cohort_changed')
    resolved = {key: binding.get(key) for key in BINDING_ENV_FIELDS}
    if not expected:
        resolved['cash_sha256'] = digest
    return {'passed': not errors, 'binding': resolved, 'evidence': evidence,
        'acceptance': acceptance, 'rejection_reasons': sorted(set(errors)),
        'can_authorize_trading': False, 'basis': 'recollected_original_estimated_paper_quote_cash_not_live_fills'}


def candidate_matches_paper(candidate: Any, sample: dict[str, Any]) -> bool:
    if not isinstance(candidate, dict) or sample.get('passed') is not True:
        return False
    score = candidate.get('objective_score')
    return (type(score) in (int, float) and math.isfinite(score) and score > 0
        and candidate.get('status') in {'accepted_replay', 'accepted_paper'}
        and _text(candidate.get('proposal_id'))
        and candidate['proposal_id'] == sample['binding'].get('proposal_id'))
