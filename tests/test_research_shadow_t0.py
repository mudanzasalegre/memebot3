"""Original inference inputs and later decision diagnostics are different data."""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_auxiliary_semantics import current_vector
from test_shadow_open_queue import function


def adapter(admit):
    namespace = {'research_runtime': SimpleNamespace(should_open_shadow=Mock(return_value=(True, 'open'))),
        '_open_shadow': admit, '_SHADOW_OPEN_QUEUE': None, 'log': SimpleNamespace(debug=Mock())}
    exec(compile(ast.Module(body=[function('_maybe_open_research_shadow')], type_ignores=[]),
        'run_bot.py', 'exec'), namespace)
    return namespace['_maybe_open_research_shadow']


CHANGES = [('entry_lane', 'pump_early_sniper_research'), ('gate_profile', 'later-gate'),
    ('liquidity_is_proxy', 1), ('mcap_bucket', 'later-bucket'),
    ('price5m_bucket', 'later-bucket'), ('venue_is_pumpswap', 1), ('impact_zero_flag', 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize('field,later', CHANGES)
async def test_native_research_adapter_does_not_rewrite_original_predictors(monkeypatch, field, later):
    from features import auxiliary_semantics as sem
    from runtime.shadow_state import snapshot_vector
    vector = current_vector(monkeypatch, 1, subprofile='sniper_research_momentum_ignition')
    original = snapshot_vector(vector)
    assert sem.checked_row_receipt(original) is not None
    assert original[field] != later
    admit = AsyncMock(return_value=True)
    assert await adapter(admit)({'address': vector['address'], field: later}, vector, reason='entry_quality')
    transported = admit.call_args.args[1]
    assert transported[field] == original[field], 'Later diagnostics replaced original inference input'
    assert sem.checked_row_receipt(transported) == sem.checked_row_receipt(original)
    assert snapshot_vector(vector) == original


@pytest.mark.asyncio
async def test_snapshot_failure_does_not_submit_original_mutable_object(monkeypatch):
    from runtime import shadow_state
    monkeypatch.setattr(shadow_state, 'snapshot_vector', Mock(side_effect=ValueError('synthetic snapshot failure')))
    admit = AsyncMock(return_value=True)
    assert not await adapter(admit)({'address': 'synthetic'}, {'address': 'synthetic'}, reason='entry_quality')
    admit.assert_not_awaited()


@pytest.mark.asyncio
async def test_later_diagnostics_are_detached_separately_including_false_and_zero(monkeypatch):
    from runtime.shadow_state import snapshot_vector
    vector = current_vector(monkeypatch, 1)
    original = snapshot_vector(vector)
    token = {'address': vector['address'], 'entry_lane': 'later-lane', 'gate_profile': 'later-gate',
        'sniper_gate_failures': ['original-failure'], 'blocked_bucket': 'later-bucket',
        'liquidity_is_proxy': False, 'impact_zero_flag': 0, 'mcap_bucket': None}
    admit = AsyncMock(return_value=True)
    assert await adapter(admit)(token, vector, reason='entry_quality')
    diagnostics = admit.call_args.kwargs['decision_context']
    assert diagnostics == {key: value for key, value in token.items() if key not in {'address', 'mcap_bucket'}}
    token['sniper_gate_failures'].append('changed')
    token['gate_profile'] = 'changed'
    assert diagnostics['sniper_gate_failures'] == ['original-failure']
    assert diagnostics['gate_profile'] == 'later-gate'
    assert admit.call_args.args[1] == original
    assert 'decision_context' not in original


@pytest.mark.asyncio
async def test_native_frontend_pins_profile_and_diagnostics_without_changing_vector(tmp_path, monkeypatch):
    from test_shadow_open_queue import frontend
    from runtime.shadow_state import snapshot_vector
    namespace = frontend(tmp_path)
    vector = current_vector(monkeypatch, 1)
    original = snapshot_vector(vector)
    diagnostics = {'entry_lane': 'later-lane', 'gate_profile': 'later-profile', 'sniper_gate_failures': ['first']}
    profile = Mock(return_value='pinned-original-shadow-profile')
    namespace['_runner_profile_for_subject'] = profile
    assert await namespace['_open_shadow'](vector['address'], vector, price_hint=1., force=True,
        decision_context=diagnostics)
    supplied = namespace['_SHADOW_OPEN_QUEUE'].submit.call_args.args[0]
    assert supplied['vector'] == original
    assert profile.call_args.args[0]['entry_lane'] == diagnostics['entry_lane']
    assert supplied['opening_snapshot']['profile'] == 'pinned-original-shadow-profile'
    assert supplied['opening_snapshot']['decision_context'] == diagnostics
    diagnostics['sniper_gate_failures'].append('later')
    assert supplied['opening_snapshot']['decision_context']['sniper_gate_failures'] == ['first']
    namespace['price_service'].get_price.assert_not_awaited()
    namespace['price_service'].get_price_usd.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('restart', [False, True])
async def test_native_queue_checkpoint_close_export_and_model_reader_keep_original_t0(tmp_path, monkeypatch, restart):
    import pandas as pd
    from features import auxiliary_semantics as sem
    from features import strategy_context as ctx
    from ml.feature_matrix import coerce_feature_frame
    from runtime import shadow_state as states
    from runtime.shadow_openings import ShadowOpeningQueue
    from test_shadow_state_recovery import native_namespace
    namespace, store, research, now = native_namespace(tmp_path, monkeypatch)
    monkeypatch.setattr(research, 'should_open_shadow', Mock(return_value=(True, 'synthetic gate only')))
    vector = current_vector(monkeypatch, 1, subprofile=ctx.VALUES[0])
    original = states.snapshot_vector(vector)
    token = {'address': vector['address'], 'price_usd': 1., 'entry_regime': 'pump_early',
        'entry_lane': 'pump_early_sniper_research', 'gate_profile': 'later-gate',
        'sniper_gate_failures': ['later-failure'], 'liquidity_is_proxy': 1, 'liquidity_usd_is_proxy': 1,
        'mcap_bucket': 'later-bucket', 'price5m_bucket': 'later-bucket', 'impact_zero_flag': 1}
    expected_diagnostics = {key: value for key, value in token.items()
        if key not in {'address', 'price_usd', 'entry_regime'}}
    expected_diagnostics = deepcopy(expected_diagnostics)
    ready = asyncio.Event()
    owner = ShadowOpeningQueue(namespace['_open_shadow_now'], ready=ready)
    owner.start()
    namespace['_SHADOW_OPEN_QUEUE'] = owner
    namespace['_runner_profile_for_subject'] = Mock(return_value='pinned-shadow-profile')
    for name in ('_open_shadow', '_maybe_open_research_shadow'):
        exec(compile(ast.Module(body=[function(name)], type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    try:
        assert await namespace['_maybe_open_research_shadow'](token, vector, reason='entry_quality')
        token['sniper_gate_failures'].append('post-admission-change')
        token['gate_profile'] = 'post-admission-change'
        vector['entry_lane'] = 'post-admission-change'
        vector.attrs.clear()
        namespace['_runner_profile_for_subject'].return_value = 'post-admission-change'
        ready.set()
    finally:
        await owner.close()
    assert owner.snapshot()['completed'] == 1
    address = original['address']
    state = namespace['_shadow_positions'][address]
    identity = state['shadow_instance_id']
    assert state['vec'] == original and state['runner_exit_profile'] == 'pinned-shadow-profile'
    payload = state['_shadow_state']['record']['opening']['payload']
    assert payload['decision_context'] == expected_diagnostics
    assert all(payload[field] == original[field] for field, _ in CHANGES)
    assert payload['sniper_gate_failures'] == ['later-failure']
    if restart:
        namespace['_shadow_positions'].clear()
        research._OPEN_SHADOWS.clear()
        restored = states.restore(tmp_path, research=research)
        assert not restored['failed']
        namespace['_shadow_positions'].update(restored['positions'])
        assert namespace['_shadow_positions'][address]['shadow_instance_id'] == identity
        assert research._OPEN_SHADOWS[address]['decision_context'] == expected_diagnostics
    namespace['utc_now'] = lambda: now + dt.timedelta(hours=4)
    namespace['exit_policy'].should_exit = lambda *args, **kwargs: 'TIMEOUT'
    await namespace['_tick_shadows']()
    assert not namespace['_shadow_positions'] and not research._OPEN_SHADOWS
    assert namespace['_stats']['appended_shadow'] == 1
    frame = pd.read_parquet(next(store.DATA_DIR.glob('features_*.parquet')))
    assert len(frame) == 1 and 'decision_context' not in frame
    assert sem.checked_row_receipt(frame.iloc[0].to_dict()) == sem.checked_row_receipt(original)
    feature_names = ['trend', f't0ctx_entry_subprofile__{ctx.VALUES[0]}']
    actual = coerce_feature_frame(sem.checked_model_frame(frame, feature_names), feature_names)
    expected = coerce_feature_frame(sem.checked_model_frame(pd.DataFrame([original]), feature_names), feature_names)
    pd.testing.assert_frame_equal(actual, expected)
    assert frame.timestamp.iat[0] == pd.Timestamp(original['timestamp']).tz_localize(None)
    rows = [json.loads(line) for line in research.RESEARCH_EVENTS_PATH.read_text().splitlines()]
    assert len(rows) == 2 and all(row['decision_context'] == expected_diagnostics for row in rows)
    research.append_decision.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('vector', [None, {'address': 'legacy', 'entry_lane': None},
    {'address': 'legacy', 'gate_profile': 'original', 'liquidity_is_proxy': 0}])
async def test_legacy_inputs_are_not_backfilled_from_later_token_metadata(vector):
    from runtime.shadow_state import snapshot_vector
    original = snapshot_vector(vector)
    admit = AsyncMock(return_value=True)
    assert await adapter(admit)({'address': 'legacy', 'entry_lane': 'later', 'gate_profile': 'later',
        'liquidity_is_proxy': 1}, vector, reason='entry_quality')
    assert admit.call_args.args[1] == original
    assert admit.call_args.kwargs['decision_context']['entry_lane'] == 'later'


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['age_minutes', 'liquidity_is_proxy'])
async def test_native_open_metadata_does_not_replace_original_zero_with_alias(tmp_path, field):
    from test_shadow_peak_observation import native_open
    namespace = native_open(tmp_path)
    vector = {'address': 'synthetic-zero', 'age_minutes': 0., 'age_min': 9.,
        'liquidity_is_proxy': 0, 'liquidity_usd_is_proxy': 1}
    assert await namespace['_open_shadow_now']('synthetic-zero', vector, price_hint=1., force=True)
    payload = namespace['_shadow_positions']['synthetic-zero']['_shadow_state']['record']['opening']['payload']
    assert payload[field] == 0, 'A genuine original zero/false became absent or an alias value'


@pytest.mark.asyncio
@pytest.mark.parametrize('diagnostics', [
    {'entry_lane': 'pump_early_sniper_research', 'gate_profile': 'sniper_research_micro_fallback'},
    {'entry_lane': 'pump_early_green_candle_sniper', 'gate_profile': 'green_sniper_birth_probe'},
    {'entry_lane': 'pump_early_late_momentum_watch', 'gate_profile': 'late_momentum_watch'}])
async def test_native_shadow_exit_profile_matches_existing_diagnostic_choice(tmp_path, monkeypatch, diagnostics):
    from analytics.exit_policy import resolve_runner_exit_profile
    from runtime.shadow_state import snapshot_vector
    from test_shadow_open_queue import frontend
    namespace = frontend(tmp_path)
    namespace['_runner_profile_for_subject'] = resolve_runner_exit_profile
    vector = current_vector(monkeypatch, 1)
    original = snapshot_vector(vector)
    expected = resolve_runner_exit_profile({**original, **diagnostics})
    assert await namespace['_open_shadow'](vector['address'], vector, force=True, decision_context=diagnostics)
    submitted = namespace['_SHADOW_OPEN_QUEUE'].submit.call_args.args[0]
    assert submitted['opening_snapshot']['profile'] == expected
    assert submitted['vector'] == original


@pytest.mark.asyncio
async def test_unserializable_diagnostics_fail_without_raw_object_fallback(tmp_path):
    from test_shadow_open_queue import frontend
    namespace = frontend(tmp_path)
    assert not await namespace['_open_shadow']('synthetic', {'address': 'synthetic'},
        force=True, decision_context={'sniper_gate_failures': object()})
    namespace['_SHADOW_OPEN_QUEUE'].submit.assert_not_called()
