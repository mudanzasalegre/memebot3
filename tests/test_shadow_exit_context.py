"""Native nonfinancial shadow exits need their original strategy context."""
from __future__ import annotations

import datetime as dt
from copy import deepcopy
from dataclasses import replace

import pytest

from test_shadow_state_recovery import native_namespace


def configuration(monkeypatch, **updates):
    from config import config
    from analytics import exit_policy
    frozen = replace(config.CFG, **updates)
    monkeypatch.setattr(config, 'CFG', frozen)
    monkeypatch.setattr(exit_policy, 'CFG', frozen)
    return frozen


@pytest.mark.asyncio
@pytest.mark.parametrize('lane,profile,minutes,price,expected', [
    ('pump_early_birth_probe_micro_canary', 'birth_probe_micro_canary', 4., 1., 'BIRTH_PROBE_NO_EXPANSION'),
    ('pump_early_green_candle_sniper', 'green_sniper', 1., .87, 'EARLY_DUMP_CUT')])
async def test_native_shadow_executes_its_actual_lane_exit(tmp_path, monkeypatch, lane, profile, minutes, price, expected):
    from analytics import exit_policy
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    ns['exit_policy'] = exit_policy
    vector = {'address': 'synthetic-lane', 'timestamp': now, 'entry_regime': 'pump_early',
        'entry_lane': lane, 'gate_profile': profile}
    assert await ns['_open_shadow'](vector['address'], vector, price_hint=1., force=True, regime='pump_early')
    current = now + dt.timedelta(minutes=minutes)
    direct = {**vector, 'opened_at': now, 'buy_price_usd': 1., 'highest_pnl_pct': 0., 'partial_taken': False}
    assert exit_policy.should_exit(direct, price, current, pnl_pct=(price-1)*100) == expected
    ns['utc_now'] = lambda: current
    ns['price_service'].get_price.return_value = {'price_usd': price}
    await ns['_tick_shadows']()
    import json
    events = [json.loads(line) for line in research.RESEARCH_EVENTS_PATH.read_text().splitlines()]
    closes = [row for row in events if row['event_type'] == 'candidate_outcome']
    assert len(closes) == 1 and closes[0]['exit_reason'] == expected, 'Native shadow lost its original lane'


@pytest.mark.asyncio
async def test_native_shadow_uses_original_proved_deep_reversal_subprofile(tmp_path, monkeypatch):
    from analytics import exit_policy
    from test_auxiliary_semantics import current_vector
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    ns['exit_policy'] = exit_policy
    vector = current_vector(monkeypatch, 1, subprofile='sniper_research_deep_reversal')
    assert await ns['_open_shadow'](vector['address'], vector, price_hint=1., force=True, regime='pump_early')
    state = ns['_shadow_positions'][vector['address']]
    ns['utc_now'] = lambda: now + dt.timedelta(seconds=10)
    ns['price_service'].get_price.return_value = {'price_usd': 1.13}
    await ns['_tick_shadows']()
    assert state.get('close_observation', {}).get('close', {}).get('exit_reason') == 'DEEP_REVERSAL_TP', \
        'Original selected subprofile never reached native shadow exit evaluation'


@pytest.mark.asyncio
async def test_shadow_original_runtime_mode_does_not_follow_later_global_override(tmp_path, monkeypatch):
    from analytics import exit_policy
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    ns['DRY_RUN'] = False
    assert await ns['_open_shadow']('synthetic-mode', {'address': 'synthetic-mode', 'timestamp': now},
        price_hint=1., force=True)
    seen = []
    def inspect(subject, *args, **kwargs):
        seen.append(exit_policy._effective_dry_run(subject))
        return None
    ns['exit_policy'].should_exit = inspect
    monkeypatch.setattr(exit_policy, '_RUNTIME_DRY_RUN_OVERRIDE', True)
    await ns['_tick_shadows']()
    assert seen == [False], 'Shadow policy inherited a later global mode instead of its original mode'


@pytest.mark.asyncio
async def test_native_shadow_uses_fractional_whole_trade_gross_return_not_remaining_spot(tmp_path, monkeypatch):
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    assert await ns['_open_shadow']('synthetic-gross', {'address': 'synthetic-gross', 'timestamp': now},
        price_hint=1., force=True)
    state = ns['_shadow_positions']['synthetic-gross']
    # Native first partial: sell half at +100%, not an invented token quantity.
    ns['exit_policy'].should_take_partial = lambda *args: True
    ns['exit_policy'].partial_fraction = lambda *args: .5
    await ns['_tick_shadows']()
    assert state['realized_pnl_ratio'] == .5 and state['remaining_fraction'] == .5
    seen = []
    ns['exit_policy'].should_take_partial = lambda *args: False
    ns['exit_policy'].should_exit = lambda subject, *args, **kwargs: seen.append(getattr(subject, 'total_pnl_pct', None))
    ns['price_service'].get_price.return_value = {'price_usd': 1.2}
    await ns['_tick_shadows']()
    assert seen == [pytest.approx(60.)], 'The original partial gross prefix was ignored by shadow protection'


@pytest.mark.asyncio
async def test_native_exit_state_mutations_survive_next_tick_and_restart(tmp_path, monkeypatch):
    from runtime import shadow_state
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    assert await ns['_open_shadow']('synthetic-exit-state', {'address': 'synthetic-exit-state', 'timestamp': now},
        price_hint=1., force=True)
    def observe(subject, *args, **kwargs):
        subject.max_adverse_pnl_pct = -7.
        subject.exit_state = 'pre_partial'
        return None
    ns['exit_policy'].should_exit = observe
    await ns['_tick_shadows']()
    ns['_shadow_positions'].clear()
    restored = shadow_state.restore(tmp_path, research=research)
    assert not restored['failed']
    state = restored['positions']['synthetic-exit-state']
    assert state.get('max_adverse_pnl_pct') == -7. and state.get('exit_state') == 'pre_partial', \
        'Native exit state was discarded before its durable checkpoint'


@pytest.mark.parametrize('paper', [False, True])
def test_original_mode_and_price_policy_are_frozen_without_financial_inputs(monkeypatch, paper):
    from runtime import shadow_exits
    from analytics import runner_price_policy
    configuration(monkeypatch, RUNNER_PRICE_TRAILING_PAPER_ENABLED=True, RUNNER_PRICE_TRAILING_DRAWDOWN_PCT=20.)
    context = shadow_exits.freeze({'entry_lane': 'original', 'qty': 123, 'entry_qty': 123,
        'realized_proceeds_usd': 999., 'entry_subprofile': 'sniper_research_deep_reversal'},
        {'entry_lane': 'original-decision'}, dry_run=paper)
    configuration(monkeypatch, RUNNER_PRICE_TRAILING_DRAWDOWN_PCT=40.)
    subject = shadow_exits.subject({'shadow_exit_context': context, 'partial_taken': True})
    assert subject.entry_lane == 'original-decision' and subject.dry_run is paper
    assert subject.entry_subprofile is None  # Arbitrary legacy column is not the v4 proof.
    assert not any(hasattr(subject, key) for key in ('qty', 'entry_qty', 'realized_proceeds_usd'))
    policy = runner_price_policy.parse_policy(subject.runner_trailing_policy)
    assert (policy is not None) is paper
    if paper: assert policy['max_price_drawdown_pct'] == 20.


@pytest.mark.asyncio
@pytest.mark.parametrize('peak', [500., 5000., 1000000.])
async def test_native_paper_mode_shadow_keeps_extreme_tail_until_price_drawdown(tmp_path, monkeypatch, peak):
    from analytics import exit_policy, runner_price_policy
    from runtime import shadow_state
    configuration(monkeypatch, RUNNER_PRICE_TRAILING_PAPER_ENABLED=True,
        RUNNER_PRICE_TRAILING_MIN_PEAK_PCT=300., RUNNER_PRICE_TRAILING_DRAWDOWN_PCT=20.,
        RUNNER_PRICE_TRAILING_MAX_HOLD_H=24., TP_PARTIAL_ENABLED=True, TP_PARTIAL_TRIGGER_PCT=25.)
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    ns['DRY_RUN'] = True
    ns['exit_policy'] = exit_policy
    vector = {'address': 'synthetic-tail', 'timestamp': now, 'entry_regime': 'pump_early', 'entry_lane': 'normal'}
    assert await ns['_open_shadow'](vector['address'], vector, price_hint=1., force=True, regime='pump_early')
    await ns['_tick_shadows']()  # Original single fractional partial at +100%.
    state = ns['_shadow_positions'][vector['address']]
    assert state['partial_taken'] and 0 < state['remaining_fraction'] < 1
    original_partial = deepcopy(state['_shadow_state']['record']['partial'])
    ns['utc_now'] = lambda: now + dt.timedelta(minutes=2)
    ns['price_service'].get_price.return_value = {'price_usd': 1 + peak / 100}
    await ns['_tick_shadows']()
    assert vector['address'] in ns['_shadow_positions']
    assert state['max_pnl_pct_seen'] == pytest.approx(peak)
    # Restart and global configuration change cannot retune this original tail.
    ns['_shadow_positions'].clear()
    research._OPEN_SHADOWS.clear()
    ns['_shadow_positions'].update(shadow_state.restore(tmp_path, research=research)['positions'])
    configuration(monkeypatch, RUNNER_PRICE_TRAILING_DRAWDOWN_PCT=5.)
    ns['price_service'].get_price.return_value = {'price_usd': (1 + peak / 100) * .95}
    await ns['_tick_shadows']()
    assert vector['address'] in ns['_shadow_positions'], 'Small price retrace prematurely lost the extreme tail'
    state = ns['_shadow_positions'][vector['address']]
    assert state['_shadow_state']['record']['partial'] == original_partial
    floor = runner_price_policy.price_drawdown_floor_pct(peak, 20.)
    ns['price_service'].get_price.return_value = {'price_usd': 1 + (floor - .01) / 100}
    await ns['_tick_shadows']()
    assert not ns['_shadow_positions']
    close = state['close_observation']['close']
    assert close['exit_reason'] == 'DYNAMIC_RUNNER_FLOOR'
    assert close['extra']['max_pnl_pct_seen'] == pytest.approx(peak)
    expected = (state['realized_pnl_ratio'] + state['remaining_fraction'] * (floor - .01) / 100) * 100
    assert close['pnl_pct'] == pytest.approx(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['mode', 'lane', 'policy'])
async def test_durable_original_exit_context_cannot_be_replaced(tmp_path, monkeypatch, mutation):
    from runtime import shadow_state
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    assert await ns['_open_shadow']('synthetic-pinned', {'address': 'synthetic-pinned', 'timestamp': now},
        price_hint=1., force=True)
    state = deepcopy(ns['_shadow_positions']['synthetic-pinned'])
    original = deepcopy(state['_shadow_state'])
    context = state['shadow_exit_context']
    if mutation == 'mode': context['dry_run'] = not context['dry_run']
    elif mutation == 'lane': context['inputs']['entry_lane'] = 'replacement'
    else:
        import json
        context['runner_trailing_policy'] = json.dumps(json.loads(context['runner_trailing_policy']), indent=1)
    with pytest.raises(ValueError, match='Original shadow entry changed'):
        shadow_state.checkpoint(tmp_path, 'synthetic-pinned', state)
    restored = shadow_state.restore(tmp_path, research=research)
    assert restored['positions']['synthetic-pinned']['_shadow_state'] == original


def test_legacy_state_does_not_gain_post_hoc_exit_context():
    from runtime import shadow_exits
    state = {'vec': {'entry_lane': 'later', 'entry_subprofile': 'sniper_research_deep_reversal'},
        'partial_taken': True, 'remaining_fraction': .5, 'realized_pnl_ratio': .5}
    original = deepcopy(state)
    subject = shadow_exits.subject(state)
    assert not hasattr(subject, 'entry_lane') and not hasattr(subject, 'dry_run')
    shadow_exits.gross_return(subject, state, 2.)
    assert not hasattr(subject, 'total_pnl_pct')
    assert not shadow_exits.retain_updates(state, subject) and state == original


@pytest.mark.parametrize('price', [None, 0., -1., float('nan'), float('inf'), True])
def test_invalid_market_reference_does_not_fabricate_shadow_wealth(price):
    from runtime import shadow_exits
    context = shadow_exits.freeze({}, {}, dry_run=False)
    state = {'shadow_exit_context': context, 'buy_price_usd': 1.,
        'remaining_fraction': .5, 'realized_pnl_ratio': .5}
    subject = shadow_exits.subject(state)
    shadow_exits.gross_return(subject, state, price)
    assert subject.total_pnl_pct is None


@pytest.mark.asyncio
async def test_failed_exit_state_checkpoint_retries_without_publishing_a_close(tmp_path, monkeypatch):
    from runtime import shadow_state
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    assert await ns['_open_shadow']('synthetic-state-io', {'address': 'synthetic-state-io', 'timestamp': now},
        price_hint=1., force=True)
    ns['exit_policy'].should_exit = lambda subject, *args, **kwargs: setattr(subject, 'max_adverse_pnl_pct', -9.)
    actual = shadow_state.checkpoint
    def fail_original_update(root, address, state, **kwargs):
        if state.get('max_adverse_pnl_pct') == -9.: raise OSError('synthetic owned state IO')
        return actual(root, address, state, **kwargs)
    with monkeypatch.context() as fault:
        fault.setattr(shadow_state, 'checkpoint', fail_original_update)
        await ns['_tick_shadows']()
    assert ns['_stats']['appended_shadow'] == 0 and not list(store.DATA_DIR.glob('*.parquet'))
    await ns['_tick_shadows']()
    restored = shadow_state.restore(tmp_path, research=research)
    assert restored['positions']['synthetic-state-io']['max_adverse_pnl_pct'] == -9.


@pytest.mark.parametrize('mutation', ['role', 'version', 'mode', 'quantity', 'subprofile_type',
    'nonfinite', 'bad_policy', 'extra_policy', 'live_paper_policy', 'nonbool_disabled', 'oversized'])
def test_invalid_context_cannot_silently_downgrade_or_add_financial_evidence(mutation):
    import json
    from runtime import shadow_exits
    from analytics.runner_price_policy import freeze_policy
    from types import SimpleNamespace
    context = shadow_exits.freeze({}, {}, dry_run=False)
    if mutation == 'role': context['role'] = 'financial_fill'
    elif mutation == 'version': context['version'] = 'invented'
    elif mutation == 'mode': context['dry_run'] = 1
    elif mutation == 'quantity': context['inputs']['entry_qty'] = 1000
    elif mutation == 'subprofile_type': context['inputs']['entry_subprofile'] = ['invented']
    elif mutation == 'nonfinite': context['inputs']['market_cap_usd'] = float('nan')
    elif mutation == 'bad_policy': context['runner_trailing_policy'] = '{}'
    elif mutation == 'extra_policy':
        policy = json.loads(context['runner_trailing_policy'])
        policy['extra'] = 1
        context['runner_trailing_policy'] = json.dumps(policy)
    elif mutation == 'live_paper_policy':
        context['runner_trailing_policy'] = freeze_policy(SimpleNamespace(
            RUNNER_PRICE_TRAILING_PAPER_ENABLED=True), dry_run=True)
    elif mutation == 'nonbool_disabled':
        policy = json.loads(context['runner_trailing_policy'])
        policy['enabled'] = 0
        context['runner_trailing_policy'] = json.dumps(policy)
    else: context['inputs']['entry_lane'] = 'x' * 65536
    with pytest.raises(ValueError): shadow_exits.validate(context)


@pytest.mark.asyncio
async def test_legacy_queued_opening_does_not_invent_missing_original_mode(tmp_path, monkeypatch):
    from runtime import shadow_exits
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    assert await ns['_open_shadow_now']('synthetic-old-queue',
        {'address': 'synthetic-old-queue', 'timestamp': now, 'entry_lane': 'later-lane'},
        price_hint=1., force=True, _opening_snapshot={'context': {}, 'profile': None, 'requested_at': now})
    state = ns['_shadow_positions']['synthetic-old-queue']
    assert state.get('shadow_exit_context') is None
    subject = shadow_exits.subject(state)
    assert not hasattr(subject, 'dry_run') and not hasattr(subject, 'entry_lane')


@pytest.mark.asyncio
async def test_native_shadow_profile_uses_the_original_supplied_rank(tmp_path, monkeypatch):
    from analytics import exit_policy
    from test_shadow_open_queue import frontend
    configuration(monkeypatch, PUMP_EARLY_PROFIT_RUNNER_JACKPOT_ENABLED=True,
        PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_LIQUIDITY_USD=10000.,
        PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_MCAP_USD=50000.,
        PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MAX_MCAP_USD=100000.,
        PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_PRICE5M_PCT=25.,
        PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MAX_PRICE5M_PCT=100.,
        PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_TXNS_5M=500,
        PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_RANK_SCORE=61.)
    namespace = frontend(tmp_path)
    namespace['_runner_profile_for_subject'] = exit_policy.resolve_runner_exit_profile
    vector = {'address': 'synthetic-rank', 'entry_lane': 'pump_early_sniper_research',
        'liquidity_usd': 15000., 'market_cap_usd': 75000., 'price_pct_5m': 50., 'txns_last_5m': 1000}
    assert exit_policy.resolve_runner_exit_profile(vector) == 'jackpot_runner'
    expected = exit_policy.resolve_runner_exit_profile({**vector, 'rank_score': 50.})
    assert expected == 'broad_runner'
    assert await namespace['_open_shadow'](vector['address'], vector, force=True, rank_info={'rank_score': 50.})
    request = namespace['_SHADOW_OPEN_QUEUE'].submit.call_args.args[0]
    assert request['opening_snapshot']['profile'] == expected, 'Original supplied rank was ignored by the shadow profile'
    assert request['opening_snapshot']['shadow_exit_context']['inputs']['rank_score'] == 50.
