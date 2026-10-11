"""Native staged bootstrap, synthetic config and observations, no bot start."""
from __future__ import annotations

import ast
import asyncio
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from analytics import paper_bootstrap as bootstrap


def config(**changes):
    values = dict(PAPER_BOOTSTRAP_ENABLED=True,
        PAPER_BOOTSTRAP_REQUIRE_COLD_START=False,
        PAPER_BOOTSTRAP_AMOUNT_SOL=.1, PAPER_BOOTSTRAP_MAX_AMOUNT_SOL=.1,
        PAPER_BOOTSTRAP_REQUIRE_EXACT_AMOUNT=True,
        PAPER_EXACT_TRADE_SIZE_ENABLED=True, PAPER_EXACT_TRADE_SIZE_SOL=.1,
        PAPER_BOOTSTRAP_BLOCK_CLUSTER_BAD=True, PAPER_BOOTSTRAP_REQUIRE_ROUTE=True,
        PAPER_BOOTSTRAP_REQUIRE_REAL_LIQUIDITY=True,
        PAPER_BOOTSTRAP_MAX_PRICE_IMPACT_PCT=12.,
        PAPER_BOOTSTRAP_MAX_OPEN=0, PAPER_BOOTSTRAP_MAX_DAILY_BUYS=0,
        PAPER_BOOTSTRAP_MAX_HOURLY_BUYS=0,
        PAPER_BOOTSTRAP_MIN_SECONDS_BETWEEN_BUYS=0.)
    return SimpleNamespace(**(values | changes))


def row(**changes):
    values = dict(address='So11111111111111111111111111111111111111112',
        chain='solana', price_usd=.01, liquidity_usd=50000.,
        market_cap_usd=100000., txns_last_5m=1000, score_total=90,
        liquidity_usd_is_proxy=False, cluster_bad=None, price_pct_5m=500.)
    return values | changes


def decide(token, **changes):
    values = dict(dry_run=True, live=False, open_count=0, daily_buys=0,
        hourly_buys=0, seconds_since_last_buy=float('inf'), closed_trades=0,
        model_loaded=False, model_rows=0, trigger_stage='pre_enrichment',
        trigger_reason='synthetic_candidate', cfg=config())
    return bootstrap.should_allow_paper_bootstrap(token, **(values | changes))


def native_helpers(cfg=None):
    """Execute the current native functions, never importing run_bot."""
    tree = ast.parse(Path('run_bot.py').read_text(encoding='utf-8'))
    names = {'_paper_bootstrap_active', '_maybe_apply_paper_bootstrap',
             '_record_paper_bootstrap_event'}
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name in names]
    assert len(functions) == len(names)
    recorded = []

    async def quotas(_):
        return 0, 0, 0, float('inf')

    async def closed(_):
        return 0

    namespace = dict(DRY_RUN=True, CFG=cfg or config(), SessionLocal=object,
        SimpleNamespace=SimpleNamespace, _PAPER_BOOTSTRAP_LANE='pump_early_paper_bootstrap_micro',
        POLICY_PAPER_BOOTSTRAP=bootstrap.POLICY_PAPER_BOOTSTRAP,
        _paper_bootstrap_quota_state=quotas, _closed_position_count=closed,
        model_runtime_status=lambda: {'model_loaded':False, 'rows':0, 'features_count':0},
        should_allow_paper_bootstrap=partial(bootstrap.should_allow_paper_bootstrap, cfg=cfg or config()),
        apply_paper_bootstrap_context=bootstrap.apply_paper_bootstrap_context,
        record_runtime_event=lambda event, address, **payload: recorded.append((event, payload)))
    exec(compile(ast.Module(body=functions, type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    return namespace, recorded


@pytest.mark.parametrize('cluster', [None, False])
def test_native_pre_enrichment_continues_to_obtain_unobserved_checks(cluster):
    ns, events = native_helpers()
    token = row(cluster_bad=cluster)
    result = asyncio.run(ns['_maybe_apply_paper_bootstrap'](token, object(), token['address'],
        trigger_stage='green_sniper', trigger_reason='shadow:synthetic'))
    assert result.allowed is True
    assert result.provisional is True and result.amount_sol == .1
    assert 'price_impact_missing' in result.pending_checks
    assert 'no_jupiter_route' in result.pending_checks
    assert ('cluster_status_unknown' in result.pending_checks) is (cluster is None)
    assert token['paper_bootstrap_provisional'] == 1
    assert events[-1][0] == 'paper_bootstrap_allowed'
    assert events[-1][1]['provisional'] is True
    assert events[-1][1]['pending_checks']


def test_native_final_revalidation_cannot_buy_with_pending_observations():
    ns, _ = native_helpers()
    token = row(paper_bootstrap=True, paper_bootstrap_provisional=1)
    decision = asyncio.run(ns['_maybe_apply_paper_bootstrap'](token, object(), token['address'],
        trigger_stage='final_execution_guard', trigger_reason='complete', revalidate_existing=True))
    assert decision.allowed is False and decision.provisional is False
    assert set(decision.hard_failures) >= {'no_jupiter_route', 'price_impact_missing', 'cluster_status_unknown'}


def test_native_completed_revalidation_clears_provisional_context():
    ns, events = native_helpers()
    token = row(paper_bootstrap=True, paper_bootstrap_provisional=1,
        paper_bootstrap_pending_checks='no_jupiter_route,price_impact_missing',
        has_jupiter_route=True, price_impact_pct=1., cluster_bad=False)
    decision = asyncio.run(ns['_maybe_apply_paper_bootstrap'](token, object(), token['address'],
        trigger_stage='post_risk_enrichment', trigger_reason='complete', revalidate_existing=True))
    assert decision.allowed is True and decision.provisional is False
    assert decision.pending_checks == () and decision.amount_sol == .1
    assert token['paper_bootstrap_provisional'] == 0
    assert token['paper_bootstrap_pending_checks'] == ''
    assert events[-1][1]['provisional'] is False


def test_standalone_default_remains_strict_before_any_opt_in():
    result = decide(row())
    assert not result.allowed
    assert set(result.hard_failures) >= {'cluster_status_unknown', 'price_impact_missing'}


@pytest.mark.parametrize('changes', [
    {'cluster_bad':True}, {'cluster_bad':'invalid'}, {'cluster_bad':''},
    {'price_impact_pct':30.}, {'price_impact_pct':'invalid'}, {'price_impact_pct':float('inf')},
    {'has_jupiter_route':False}, {'has_jupiter_route':'invalid'},
    {'liquidity_usd_is_proxy':True}, {'liquidity_usd':0.},
    {'market_cap_usd':0.}, {'txns_last_5m':0}, {'score_total':0},
    {'price_pct_5m':None}, {'banned_creator':True}, {'toxic_initial_sell_pressure':True},
])
def test_provisional_enrichment_never_erases_known_invalid_or_failed_input(changes):
    assert not decide(row(**changes), allow_pending_enrichment=True).allowed


@pytest.mark.parametrize('momentum', [500., 25000., 1000000.])
def test_provisional_and_strict_completed_phases_keep_extreme_signal_and_exact_size(momentum):
    token = row(price_pct_5m=momentum)
    provisional = decide(token, allow_pending_enrichment=True)
    assert provisional.allowed and provisional.provisional and provisional.amount_sol == .1
    completed = decide(token | dict(cluster_bad=False, price_impact_pct=1., has_jupiter_route=True),
        require_observed_route=True)
    assert completed.allowed and not completed.provisional and completed.amount_sol == .1
    assert token['price_pct_5m'] == momentum


@pytest.mark.parametrize('value', [1, 'true', None])
def test_pending_mode_requires_explicit_boolean(value):
    with pytest.raises(ValueError, match='explicitly boolean'):
        decide(row(), allow_pending_enrichment=value)


def test_final_mode_cannot_opt_into_pending_enrichment():
    with pytest.raises(ValueError, match='final'):
        decide(row(), require_observed_route=True, allow_pending_enrichment=True)


@pytest.mark.parametrize('dry_run,live', [(False, True), (False, False), (True, True)])
def test_provisional_mode_is_never_live_permission(dry_run, live):
    result = decide(row(), dry_run=dry_run, live=live, allow_pending_enrichment=True)
    assert not result.allowed and result.reason == 'paper_bootstrap_paper_only'


def test_native_buy_path_still_revalidates_after_risk_route_and_before_execution():
    tree = ast.parse(Path('run_bot.py').read_text(encoding='utf-8'))
    native = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == '_evaluate_and_buy')
    calls = [n for n in ast.walk(native) if isinstance(n, ast.Call)]
    revalidated = [n for n in calls if isinstance(n.func, ast.Name)
        and n.func.id == '_maybe_apply_paper_bootstrap'
        and any(k.arg == 'revalidate_existing' and isinstance(k.value, ast.Constant)
                and k.value.value is True for k in n.keywords)]
    stages = {next(k.value.value for k in n.keywords if k.arg == 'trigger_stage') for n in revalidated}
    assert stages >= {'post_risk_enrichment', 'final_execution_guard'}
    final = next(n for n in revalidated if any(k.arg == 'trigger_stage'
        and isinstance(k.value, ast.Constant) and k.value.value == 'final_execution_guard'
        for k in n.keywords))
    buy = next(n for n in calls if isinstance(n.func, ast.Attribute)
               and isinstance(n.func.value, ast.Name) and n.func.value.id == 'buyer'
               and n.func.attr == 'buy')
    assert final.lineno < buy.lineno


def native_guard(token, stage, *, overlay_allowed=True, capacity_allowed=True):
    """Execute the entire native guard, with inert dependencies and sentinels.

    This is a bounded guard test, not a running bot or a simulated provider.
    The sentinels identify whether the guard returns before downstream work.
    """
    ns, events = native_helpers()
    tree = ast.parse(Path('run_bot.py').read_text(encoding='utf-8'))
    native = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == '_evaluate_and_buy')
    guard = next(n for n in native.body if isinstance(n, ast.If)
        and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            and child.func.id == '_maybe_apply_paper_bootstrap'
            and any(k.arg == 'trigger_stage' and isinstance(k.value, ast.Constant)
                and k.value.value == stage for k in child.keywords)
            for child in ast.walk(n)))
    calls = []

    async def shadow(*args, **kwargs):
        calls.append('shadow')

    async def capacity(*args):
        calls.append('capacity')
        return capacity_allowed, 1, 1

    def overlay(*args, **kwargs):
        calls.append('overlay')
        return SimpleNamespace(allowed=overlay_allowed, reason='synthetic_overlay', backoff_s=60)

    ns.update(token=token, ses=object(), addr=token['address'],
        paper_bootstrap_fast_path=True, _stats={'filtered_out':0},
        build_feature_vector=lambda token: dict(token),
        _store_policy_reject=lambda *a, **k: calls.append('reject'),
        _research_decision=lambda *a, **k: None, _open_shadow=shadow,
        _remember_stream_candidate_cooldown=lambda *a: None,
        _stream_candidate_cooldown_s=lambda *a: 0,
        _remove_from_queue_if_present=lambda *a: calls.append('remove'),
        _requeue_with_stats=lambda *a, **k: calls.append('requeue'),
        evaluate_policy_overlay=overlay, _lane_capacity=capacity,
        PROJECT_ROOT=Path('.'), proba=None, ai_threshold_eff=None, rank_info=None,
        persisted=lambda: calls.append('persist_sentinel'),
        bought=lambda: calls.append('buy_sentinel'))
    sentinels = [ast.Expr(value=ast.Call(func=ast.Name(id=name, ctx=ast.Load()),
        args=[], keywords=[])) for name in ('persisted', 'bought')]
    wrapper = ast.AsyncFunctionDef(name='check_native_guard',
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[guard, *sentinels], decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
        'run_bot.py', 'exec'), ns)
    asyncio.run(ns['check_native_guard']())
    return ns, events, calls


@pytest.mark.parametrize('stage', ['post_risk_enrichment', 'final_execution_guard'])
@pytest.mark.parametrize('changes', [
    {'has_jupiter_route':None}, {'has_jupiter_route':False},
    {'cluster_bad':None}, {'cluster_bad':True},
    {'price_impact_pct':None}, {'price_impact_pct':30.}, {'price_impact_pct':'invalid'},
])
def test_executed_native_strict_guards_return_before_persistence_and_buy(stage, changes):
    token = row(paper_bootstrap=True, paper_bootstrap_provisional=1,
        has_jupiter_route=True, cluster_bad=False, price_impact_pct=1.) | changes
    ns, events, calls = native_guard(token, stage)
    assert calls == ['reject', 'shadow', 'remove']
    assert ns['_stats']['filtered_out'] == 1
    assert events[-1][0] == 'paper_bootstrap_blocked'
    assert events[-1][1]['provisional'] is False


@pytest.mark.parametrize('stage', ['post_risk_enrichment', 'final_execution_guard'])
def test_executed_native_complete_guard_clears_pending_before_downstream_sentinels(stage):
    token = row(paper_bootstrap=True, paper_bootstrap_provisional=1,
        paper_bootstrap_pending_checks='no_jupiter_route',
        has_jupiter_route=True, cluster_bad=False, price_impact_pct=1.)
    ns, events, calls = native_guard(token, stage)
    expected_prefix = ['overlay', 'capacity'] if stage == 'final_execution_guard' else []
    assert calls == expected_prefix + ['persist_sentinel', 'buy_sentinel']
    assert ns['_stats']['filtered_out'] == 0
    assert token['paper_bootstrap_provisional'] == 0
    assert token['paper_bootstrap_pending_checks'] == ''
    assert events[-1][1]['provisional'] is False


@pytest.mark.parametrize('failure', ['overlay', 'capacity'])
def test_executed_final_guard_preserves_overlay_and_capacity_vetoes(failure):
    token = row(paper_bootstrap=True, has_jupiter_route=True,
        cluster_bad=False, price_impact_pct=1.)
    _, _, calls = native_guard(token, 'final_execution_guard',
        overlay_allowed=failure != 'overlay', capacity_allowed=failure != 'capacity')
    assert calls == (['overlay', 'requeue'] if failure == 'overlay'
                     else ['overlay', 'capacity', 'requeue'])
