"""Reuse observed shadow market updates for discovery, never outcomes or orders.

No IO, workers, subscription changes, predictor calls or mutable global state.
The normal queue owns capacity/dedup/rechecks; native entry must recollect all
decision observations and still pass its independent admission/risk gates.
"""
from __future__ import annotations

from copy import deepcopy
import time

from analytics.token_birth import FIELD, base58_size, birth_clock, token_birth_problem
from analytics.token_time import BIRTH_CLOCK_FIELDS, _to_datetime
from features.token_clock_semantics import checked_row_clock
from runtime.entry_observation import prepare_entry_candidate


def shadow_market_candidate(address, state, market):
    """Only original clock context plus a current address-bound market receipt.

    Legacy/malformed/unanchored original clocks stay unavailable. A shadow
    return, peak, label, T0 predictor or closing value never supplies momentum.
    The preserved birth clock is not independent chain/provider authentication.
    """
    if (not base58_size(address, 32) or not isinstance(state, dict)
            or state.get('opening_pending') or 'close_observation' in state
            or not isinstance(state.get('vec'), dict)):
        return None
    try:
        now = time.time()
        opened = _to_datetime(state.get('opened_at'))
        original = checked_row_clock(state['vec'])
        if (original is None or original['address'] != address or opened is None
                or opened.timestamp() > now
                or _to_datetime(original['captured_at']).timestamp() > opened.timestamp()):
            return None
        context = {'address': address, 'source': 'shadow_followup', 'discovered_via': 'shadow_followup'}
        for key in BIRTH_CLOCK_FIELDS:
            if original['inputs'].get(key) is not None:
                context[key] = deepcopy(original['inputs'][key])
        if FIELD in original:
            context[FIELD] = deepcopy(original[FIELD])
        born = birth_clock(context)
        if born is None or born.timestamp() > now or token_birth_problem(context, now=now) is not None:
            return None
        candidate = prepare_entry_candidate(context, market)
        if (candidate is None or birth_clock(candidate) != born
                or token_birth_problem(candidate, now=now) is not None):
            return None
        # The new observation source is the shadow monitor, not a claim that
        # another PumpPortal creation event or an original prediction arrived.
        candidate['source'] = candidate['discovered_via'] = 'shadow_followup'
        return candidate
    except (ValueError, TypeError, KeyError, RuntimeError, OverflowError, RecursionError):
        return None
