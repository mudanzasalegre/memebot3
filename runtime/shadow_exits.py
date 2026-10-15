"""Frozen exit inputs for unquoted, nonfinancial fractional shadows.

These subjects deliberately have no token quantities, fills, fees or net cash.
They cannot activate the quantity-based multi-partial financial machinery.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from types import SimpleNamespace

VERSION = 'unquoted_fractional_shadow_exits_v1'
FIELDS = frozenset(('entry_lane', 'gate_profile', 'profit_lane_tier', 'sniper_gate_profile',
    'dex_id', 'liquidity_is_proxy', 'liquidity_usd_is_proxy', 'liquidity_usd',
    'market_cap_usd', 'price_pct_5m', 'txns_last_5m', 'discovered_via',
    'rank_score', 'research_rank_score', 'size_bucket', 'entry_subprofile',
    'sniper_research_subprofile'))
KEYS = {'version', 'role', 'partial_model', 'inputs', 'dry_run', 'runner_trailing_policy'}


def validate(context):
    if (not isinstance(context, dict) or set(context) != KEYS or context['version'] != VERSION
            or context['role'] != 'nonfinancial_shadow_only'
            or context['partial_model'] != 'single_fractional_partial_without_quantities_or_costs'
            or type(context['dry_run']) is not bool or not isinstance(context['inputs'], dict)
            or not set(context['inputs']) <= FIELDS or not isinstance(context['runner_trailing_policy'], str)):
        raise ValueError('Invalid original shadow exit context')
    for value in context['inputs'].values():
        if value is not None and (type(value) not in (str, bool, int, float)
                or type(value) is float and not math.isfinite(value)):
            raise ValueError('Invalid shadow exit input')
    from analytics.runner_price_policy import parse_policy, VERSION as PRICE_POLICY_VERSION
    try:
        policy = json.loads(context['runner_trailing_policy'])
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid original shadow price policy') from exc
    disabled = {'version': PRICE_POLICY_VERSION, 'role': 'paper_exit_only', 'enabled': False}
    parsed = parse_policy(policy)
    is_disabled = policy == disabled and policy.get('enabled') is False
    if not is_disabled and (not context['dry_run'] or parsed is None or policy != parsed):
        raise ValueError('Invalid original shadow price policy')
    if len(json.dumps(context, allow_nan=False).encode('utf-8')) > 65536:
        raise ValueError('Oversized shadow exit context')
    return context


def freeze(vector, diagnostics, *, dry_run, rank_info=None):
    """No providers, IO, new T0 or reconstruction from a reason/outcome."""
    if type(dry_run) is not bool:
        raise ValueError('Invalid original shadow runtime mode')
    from runtime.shadow_state import snapshot_vector
    from features.strategy_context import original_context
    from analytics.runner_price_policy import freeze_policy
    from config.config import CFG
    values = {**(vector or {}), **(diagnostics or {})}
    if rank_info is not None:
        if not isinstance(rank_info, dict): raise ValueError('Invalid original shadow rank')
        values.update({key: rank_info[key] for key in ('rank_score', 'research_rank_score') if key in rank_info})
    inputs = {key: values[key] for key in FIELDS if key in values}
    # Only the original v4 receipt proves a selected strategy subprofile.
    # A later token alias, arbitrary column or reason cannot manufacture it.
    proof = original_context(vector or {})
    selected = proof['strategy_context']['entry_subprofile'] if proof is not None else None
    inputs['entry_subprofile'] = inputs['sniper_research_subprofile'] = selected
    return validate({'version': VERSION, 'role': 'nonfinancial_shadow_only',
        'partial_model': 'single_fractional_partial_without_quantities_or_costs',
        'inputs': snapshot_vector(inputs), 'dry_run': dry_run,
        'runner_trailing_policy': freeze_policy(CFG, dry_run=dry_run)})


def subject(state):
    context = state.get('shadow_exit_context')
    values = {}
    if context is not None:
        validate(context)
        values = {**deepcopy(context['inputs']), 'dry_run': context['dry_run'],
            'runner_trailing_policy': context['runner_trailing_policy']}
    # Legacy states keep their original reduced input contract. Do not infer
    # or backfill a new generation from their current vector or portfolio.
    values.update({key: state.get(key) for key in ('entry_regime', 'opened_at', 'buy_price_usd',
        'buy_liquidity_usd', 'highest_pnl_pct', 'max_pnl_pct_seen', 'runner_exit_profile',
        'max_adverse_pnl_pct', 'exit_state')})
    values['partial_taken'] = bool(state.get('partial_taken'))
    return SimpleNamespace(**values)


def gross_return(subject, state, price):
    """Original fractional gross wealth, explicitly not PAPER net cash."""
    if state.get('shadow_exit_context') is None:
        return  # Preserve the original legacy simulation generation.
    from runtime.shadow_closes import positive_price
    entry, current = positive_price(state.get('buy_price_usd')), positive_price(price)
    result = None
    if entry is not None and current is not None:
        remaining = state.get('remaining_fraction', 1.)
        realized = state.get('realized_pnl_ratio', 0.)
        if (type(remaining) in (int, float) and type(realized) in (int, float)
                and math.isfinite(remaining) and math.isfinite(realized) and 0 <= remaining <= 1):
            value = (realized + remaining * ((current - entry) / entry)) * 100.
            if math.isfinite(value): result = value
    subject.total_pnl_pct = result


def retain_updates(state, evaluated):
    """Persist only native diagnostic exit state, never source/financial fields."""
    if state.get('shadow_exit_context') is None:
        return False
    changed = False
    for key in ('highest_pnl_pct', 'max_pnl_pct_seen', 'max_adverse_pnl_pct', 'exit_state'):
        value = getattr(evaluated, key, None)
        if value is None: continue
        if key == 'exit_state':
            if value not in {'pre_partial', 'post_partial'}: raise ValueError('Invalid shadow exit phase')
        elif type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('Invalid shadow exit observation')
        if state.get(key) != value:
            state[key] = value
            changed = True
    return changed
