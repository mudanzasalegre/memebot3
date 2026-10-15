"""Nonfinancial shadow admission must not retain the entry evaluator."""
from __future__ import annotations

import ast
import asyncio
import datetime as dt
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def function(name):
    return next(node for node in ast.parse(Path('run_bot.py').read_text(encoding='utf-8')).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name)


def frontend(root):
    from test_shadow_peak_observation import native_open
    namespace = native_open(root)
    namespace.update(_SHADOW_OPEN_QUEUE=SimpleNamespace(submit=Mock(return_value=True),
        snapshot=lambda: {}, pending_tokens=lambda: {}), _stats={})
    exec(compile(ast.Module(body=[function('_open_shadow')], type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    return namespace


@pytest.mark.asyncio
async def test_native_admission_returns_without_waiting_for_shadow_price(tmp_path):
    namespace = frontend(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return 1.
    namespace['price_service'].get_price_usd = AsyncMock(side_effect=held)
    admission = asyncio.create_task(namespace['_open_shadow']('synthetic-entry-not-blocked',
        {'address': 'synthetic-entry-not-blocked'}, force=True))
    price_started = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait({admission, price_started}, timeout=2,
            return_when=asyncio.FIRST_COMPLETED)
        assert admission in done, 'Auxiliary price lookup retained the entry caller'
        assert admission.result() is True
        namespace['price_service'].get_price_usd.assert_not_awaited()
        assert namespace['_SHADOW_OPEN_QUEUE'].submit.call_count == 1
    finally:
        release.set()
        price_started.cancel()
        await asyncio.gather(admission, price_started, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_research_shadow_keeps_original_feature_proof_attrs():
    import pandas as pd
    from features.auxiliary_semantics import PROOF_COLUMN
    from features.token_clock_semantics import PROOF_COLUMN as CLOCK
    from features.valuation_semantics import PROOF_COLUMN as VALUATION
    vector = pd.Series({'address': 'synthetic-attrs', 'score_total': 73.})
    vector.attrs = {key: {'synthetic_marker': key} for key in (PROOF_COLUMN, CLOCK, VALUATION)}
    admit = AsyncMock(return_value=True)
    namespace = {'research_runtime': SimpleNamespace(should_open_shadow=Mock(return_value=(True, 'open'))),
        '_open_shadow': admit, '_SHADOW_OPEN_QUEUE': None, 'log': SimpleNamespace(debug=Mock())}
    exec(compile(ast.Module(body=[function('_maybe_open_research_shadow')], type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    assert await namespace['_maybe_open_research_shadow']({'address': 'synthetic-attrs'}, vector, reason='soft_score')
    supplied = admit.call_args.args[1]
    assert all(supplied.get(key) == vector.attrs[key] for key in vector.attrs), 'Original proof attrs were stripped'


def request(address='synthetic-queue'):
    return {'address': address, 'vector': {'address': address, 'timestamp': '2026-10-15T00:00:00Z', 'nullable': None},
        'price_hint': 1., 'force': True, 'regime': 'pump_early', 'reason': 'original',
        'stage': 'decision', 'proba': None, 'threshold': .7, 'rank_info': {'rank_score': 73.},
        'shadow_kind': 'research', 'opening_snapshot': {'context': {'run_id': 'ORIGINAL-SYNTHETIC'},
            'profile': 'original-profile', 'requested_at': '2026-10-15T00:00:00Z'}}


def queue(handler, *, ready=True, **kwargs):
    from runtime.shadow_openings import ShadowOpeningQueue
    event = asyncio.Event()
    if ready: event.set()
    instance = ShadowOpeningQueue(handler, ready=event, **kwargs)
    instance.start()
    return instance


@pytest.mark.asyncio
async def test_serial_owner_detaches_inputs_and_counts_active_capacity():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(address, vector, **kwargs):
        calls.append((address, vector, kwargs))
        entered.set()
        await release.wait()
        return True
    owner = queue(handler, capacity=1)
    original = request()
    assert owner.submit(original)
    original['vector']['nullable'] = 999.
    original['opening_snapshot']['context']['run_id'] = 'CHANGED'
    await entered.wait()
    assert owner.snapshot()['pending'] == 1
    assert not owner.submit(request('second')) and owner.snapshot()['last_drop_reason'] == 'capacity'
    release.set()
    await owner.close()
    assert calls[0][1]['nullable'] is None
    assert calls[0][2]['_opening_snapshot']['context']['run_id'] == 'ORIGINAL-SYNTHETIC'
    assert owner.snapshot()['completed'] == 1 and owner.snapshot()['closed']


@pytest.mark.asyncio
async def test_duplicate_pending_address_does_not_replace_original_vector():
    calls = []
    owner = queue(AsyncMock(side_effect=lambda *args, **kwargs: None), ready=False)
    first = request()
    assert owner.submit(first)
    assert not owner.submit({**request(), 'reason': 'replacement'})
    assert owner.snapshot()['last_drop_reason'] == 'duplicate'
    assert owner.pending_tokens() == {'synthetic-queue': {'regime': 'pump_early', 'shadow_kind': 'research'}}
    await owner.close()
    assert owner.snapshot()['failed'] == 1 and owner.snapshot()['pending'] == 0


@pytest.mark.asyncio
async def test_work_waits_for_bootstrap_and_shutdown_before_ready_does_no_io():
    handler = AsyncMock(return_value=True)
    owner = queue(handler, ready=False)
    assert owner.submit(request())
    await asyncio.sleep(0)
    handler.assert_not_awaited()
    await owner.close()
    handler.assert_not_awaited()
    assert owner.snapshot()['failed'] == 1 and owner.snapshot()['closed']


@pytest.mark.asyncio
async def test_ready_transition_releases_one_owned_consumer():
    handler = AsyncMock(return_value=True)
    owner = queue(handler, ready=False)
    assert owner.submit(request())
    await asyncio.sleep(0)
    handler.assert_not_awaited()
    owner.ready.set()
    await owner.close()
    assert handler.await_count == 1 and owner.snapshot()['completed'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('result', [False, None, 1])
async def test_only_true_confirms_handler_completion(result):
    owner = queue(AsyncMock(return_value=result))
    assert owner.submit(request())
    await owner.close()
    assert owner.snapshot()['failed'] == 1 and owner.snapshot()['completed'] == 0


@pytest.mark.asyncio
async def test_one_failure_does_not_abandon_next_shadow():
    owner = queue(AsyncMock(side_effect=[OSError('synthetic IO'), True]))
    assert owner.submit(request()) and owner.submit(request('second'))
    await owner.close()
    assert owner.snapshot()['failed'] == owner.snapshot()['completed'] == 1


@pytest.mark.asyncio
async def test_stale_memory_job_expires_without_inventing_an_observation():
    clock = [0.]
    handler = AsyncMock(return_value=True)
    owner = queue(handler, ready=False, max_wait_s=1., clock=lambda: clock[0])
    assert owner.submit(request())
    clock[0] = 2.
    owner.ready.set()
    await owner.close()
    handler.assert_not_awaited()
    assert owner.snapshot()['expired'] == owner.snapshot()['failed'] == 1


@pytest.mark.asyncio
async def test_handler_timeout_awaits_its_cleanup():
    cleaned = asyncio.Event()
    async def handler(*args, **kwargs):
        try: await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()
    owner = queue(handler, timeout_s=.01)
    assert owner.submit(request())
    await owner.close()
    assert cleaned.is_set() and owner.snapshot()['failed'] == 1 and owner.snapshot()['closed']


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['closer', 'worker'])
async def test_repeated_cancellation_drains_the_original_owned_operation(target):
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(*args, **kwargs):
        entered.set()
        await release.wait()
        return True
    owner = queue(handler)
    assert owner.submit(request())
    await entered.wait()
    closing = asyncio.create_task(owner.close())
    await asyncio.sleep(0)
    cancelled = closing if target == 'closer' else owner._task
    cancelled.cancel()
    await asyncio.sleep(0)
    cancelled.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and not owner.snapshot()['closed']
    release.set()
    if target == 'closer':
        with pytest.raises(asyncio.CancelledError): await closing
    else: await closing
    assert owner.snapshot()['completed'] == 1 and owner.snapshot()['pending'] == 0


@pytest.mark.asyncio
async def test_foreign_thread_cannot_submit_or_mutate_owner_counters():
    owner = queue(AsyncMock(return_value=True))
    before = owner.snapshot()
    result = await asyncio.to_thread(owner.submit, request())
    assert result is False and owner.snapshot() == before
    await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('force', 1), ('vector', []), ('opening_snapshot', None),
    ('address', ''), ('rank_info', {'huge': 'x'*2097153}), ('rank_info', {'huge': 1 << 5000})])
async def test_invalid_request_is_rejected_without_work(field, value):
    handler = AsyncMock(return_value=True)
    owner = queue(handler)
    assert not owner.submit({**request(), field: value})
    assert owner.snapshot()['last_drop_reason'] == 'invalid_snapshot'
    await owner.close()
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('options', [{'capacity': True}, {'capacity': 0}, {'capacity': 257},
    {'max_bytes': 0}, {'timeout_s': float('nan')}, {'timeout_s': True}, {'max_wait_s': -1}])
async def test_invalid_owner_budgets_are_explicit_errors(options):
    from runtime.shadow_openings import ShadowOpeningQueue
    with pytest.raises(ValueError): ShadowOpeningQueue(AsyncMock(), ready=asyncio.Event(), **options)


@pytest.mark.asyncio
async def test_failed_worker_task_creation_drains_its_readiness_owner(monkeypatch):
    from runtime.shadow_openings import ShadowOpeningQueue
    loop = asyncio.get_running_loop()
    original = loop.create_task
    def create(coro, *args, **kwargs):
        if kwargs.get('name') == 'shadow-opening-owner': raise RuntimeError('synthetic startup failure')
        return original(coro, *args, **kwargs)
    owner = ShadowOpeningQueue(AsyncMock(), ready=asyncio.Event())
    with monkeypatch.context() as fault:
        fault.setattr(loop, 'create_task', create)
        with pytest.raises(RuntimeError, match='startup failure'): owner.start()
    await owner.close()
    assert owner.snapshot()['closed'] and owner._ready_task.done()


@pytest.mark.asyncio
async def test_native_queue_preserves_original_context_profile_and_t0(tmp_path, monkeypatch):
    from test_shadow_state_recovery import native_namespace
    from utils import runtime_context
    namespace, store, research, now = native_namespace(tmp_path, monkeypatch)
    namespace['_stats'] = {}
    exec(compile(ast.Module(body=[function('_open_shadow')], type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    owner = queue(namespace['_open_shadow_now'], ready=False)
    namespace['_SHADOW_OPEN_QUEUE'] = owner
    namespace['_runner_profile_for_subject'] = lambda subject: 'original-profile'
    monkeypatch.setattr(runtime_context, 'runtime_context_payload', lambda: {'run_id': 'ORIGINAL-SYNTHETIC'})
    vector = {'address': 'synthetic-native-queue', 'timestamp': now, 'score_total': 73.}
    assert await namespace['_open_shadow'](vector['address'], vector, price_hint=1., force=True)
    assert not namespace['_shadow_positions']
    vector['score_total'] = -999.
    namespace['_runner_profile_for_subject'] = lambda subject: 'changed-profile'
    monkeypatch.setattr(runtime_context, 'runtime_context_payload', lambda: {'run_id': 'CHANGED'})
    owner.ready.set()
    await owner.close()
    state = namespace['_shadow_positions'][vector['address']]
    assert state['vec']['score_total'] == 73. and state['runner_exit_profile'] == 'original-profile'
    assert state['vec']['timestamp'] == now.isoformat()
    assert state['_shadow_state']['record']['context']['run_id'] == 'ORIGINAL-SYNTHETIC'
    assert state['shadow_requested_at'] == now.isoformat()
    assert state['shadow_open_latency_sec'] == 0.
    assert owner.snapshot()['completed'] == 1 and not list(store.DATA_DIR.glob('*.parquet'))


def test_pending_admission_reserves_existing_research_limits_without_double_count(monkeypatch):
    from analytics import research_runtime as research
    original = {'a': {'regime': 'pump_early'}}
    monkeypatch.setattr(research, '_OPEN_SHADOWS', original)
    monkeypatch.setattr(research, 'CFG', SimpleNamespace(RESEARCH_SHADOW_MAX_OPEN=2,
        RESEARCH_SHADOW_MAX_OPEN_PER_REGIME=2, RESEARCH_SHADOW_MIN_AGE_MIN=1,
        RESEARCH_SHADOW_MIN_LIQUIDITY_USD=1, RESEARCH_SHADOW_MIN_RANK_SCORE=1))
    token = {'address': 'c', 'entry_regime': 'pump_early', 'age_minutes': 3., 'liquidity_usd': 1000.}
    options = dict(action='rejected', reason='soft_score', proba=.8, threshold=.7,
        rank_info={'rank_score': 73.})
    assert research.should_open_shadow(token, **options, pending_shadows={'a': {'regime': 'pump_early'}})[0]
    assert research.should_open_shadow(token, **options,
        pending_shadows={'a': {'regime': 'pump_early'}, 'b': {'regime': 'pump_early'}}) == (False, 'shadow_cap_global')


@pytest.mark.asyncio
async def test_native_unowned_admission_cannot_start_provider_work(tmp_path):
    namespace = frontend(tmp_path)
    namespace['_SHADOW_OPEN_QUEUE'] = None
    assert await namespace['_open_shadow']('synthetic-no-owner', {'address': 'synthetic-no-owner'}, force=True) is False
    namespace['price_service'].get_price_usd.assert_not_awaited()
    namespace['price_service'].get_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_research_result_reports_failed_admission():
    admit = AsyncMock(return_value=False)
    namespace = {'research_runtime': SimpleNamespace(should_open_shadow=Mock(return_value=(True, 'open'))),
        '_open_shadow': admit, '_SHADOW_OPEN_QUEUE': None, 'log': SimpleNamespace(debug=Mock())}
    exec(compile(ast.Module(body=[function('_maybe_open_research_shadow')], type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    assert await namespace['_maybe_open_research_shadow']({'address': 'synthetic-full'},
        {'address': 'synthetic-full'}, reason='soft_score') is False


@pytest.mark.asyncio
async def test_fatal_owner_releases_pending_and_watch_reports_original_failure():
    class Fatal(BaseException): pass
    async def handler(*args, **kwargs): raise Fatal('synthetic fatal shadow')
    owner = queue(handler)
    assert owner.submit(request()) and owner.submit(request('second'))
    with pytest.raises(Fatal, match='synthetic fatal shadow'): await owner.watch()
    with pytest.raises(Fatal, match='synthetic fatal shadow'): await owner.close()
    assert owner.snapshot()['closed'] and owner.snapshot()['failed'] == 2 and not owner.pending_tokens()


@pytest.mark.asyncio
async def test_callback_exception_cannot_stop_later_shadow_work():
    def callback(*args): raise ValueError('synthetic callback')
    owner = queue(AsyncMock(return_value=True), on_result=callback)
    assert owner.submit(request()) and owner.submit(request('second'))
    await owner.close()
    assert owner.snapshot()['completed'] == owner.snapshot()['callback_errors'] == 2


@pytest.mark.asyncio
async def test_actual_runner_drains_shadow_provider_owner_before_stopped(tmp_path, monkeypatch):
    from test_shadow_state_recovery import native_namespace
    from runtime import social_enrichment_queue
    from research_loop import entry_gate_forward
    namespace, store, research, now = native_namespace(tmp_path, monkeypatch)
    entered, release, entry_progress, closing = (asyncio.Event() for _ in range(4))
    published = []
    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return 1.
    namespace['price_service'].get_price_usd = AsyncMock(side_effect=held)
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    async def forever(*args, **kwargs): await asyncio.Event().wait()
    async def main(*, positions_ready, **kwargs):
        positions_ready.set()
        assert await namespace['_open_shadow']('synthetic-owned-runtime',
            {'address': 'synthetic-owned-runtime', 'timestamp': now}, force=True)
        entry_progress.set()
        await forever()
    async def fault():
        await entered.wait()
        closing.set()
        raise RuntimeError('synthetic runtime stop')
    async def stopped():
        assert namespace['_SHADOW_OPEN_QUEUE'] is None
        assert namespace['_runtime_process_state'] == 'stopped'
        assert namespace['_stats']['shadow_open_pending'] == 0
        assert namespace['_stats']['shadow_open_completed'] == 1
        assert namespace['_stats']['shadow_open_closed'] is True
        published.append(True)
    monkeypatch.setattr(social_enrichment_queue, 'stop_background_tasks', AsyncMock())
    monkeypatch.setattr(entry_gate_forward, 'stop_background_tasks', AsyncMock())
    namespace.update(asyncio=asyncio, CFG=SimpleNamespace(ML_RETRAIN_IN_MAIN_LOOP=False, HOT_QUEUE_ENABLED=False),
        DRY_RUN=False, async_init_db=AsyncMock(), SessionLocal=Session,
        _recover_buy_persistence_outbox=AsyncMock(), _recover_close_persistence_outbox=AsyncMock(),
        _refresh_green_live_risk=AsyncMock(), _policy_feature_write_result=Mock(),
        main_loop=main, _dex_discovery_loop=forever, _position_monitor_loop=forever,
        _shadow_monitor_loop=forever, _periodic_labeler=forever, control_command_loop=fault,
        runtime_state_loop=forever, _background_tasks=set(), _publish_runtime_state_once=stopped,
        pumpfun=SimpleNamespace(stop_background_tasks=AsyncMock()))
    exec(compile(ast.Module(body=[function('_open_shadow'), function('_runner')], type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    task = asyncio.create_task(namespace['_runner']())
    try:
        await asyncio.wait_for(closing.wait(), 2)
        assert entry_progress.is_set()
        for _ in range(8): await asyncio.sleep(0)
        assert not task.done() and not published
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not published
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert published == [True]
    assert 'synthetic-owned-runtime' in research._OPEN_SHADOWS
    assert len(list((tmp_path/'data/research/shadow_state/active').glob('*.json'))) == 1
    assert not list(store.DATA_DIR.glob('*.parquet'))


def test_cold_queue_import_has_no_provider_worker_or_disk_effect(tmp_path):
    import subprocess
    import sys
    code = '''
import json, socket, subprocess, sys, threading
from pathlib import Path
sys.path.insert(0, sys.argv[1])
def denied(event, args):
 if event in {'socket.__new__', 'socket.connect', 'subprocess.Popen'}:
  raise AssertionError('Import attempted external work')
sys.addaudithook(denied)
before=len(threading.enumerate())
import runtime.shadow_openings
assert len(threading.enumerate()) == before and not list(Path.cwd().iterdir())
print(json.dumps({'new_threads':0, 'disk_entries':0}))
'''
    child = subprocess.run([sys.executable, '-c', code, str(Path.cwd())], cwd=tmp_path,
        text=True, capture_output=True, timeout=30, check=True)
    import json
    assert json.loads(child.stdout.strip()) == {'new_threads': 0, 'disk_entries': 0}


def test_research_capacity_uses_a_detached_address_snapshot(monkeypatch):
    from analytics import research_runtime as research
    class LiveMap(dict):
        def values(self): raise AssertionError('Admission iterated the live IO-owned map')
    monkeypatch.setattr(research, '_OPEN_SHADOWS', LiveMap({'existing': {'regime': 'pump_early'}}))
    monkeypatch.setattr(research, 'CFG', SimpleNamespace(RESEARCH_SHADOW_MAX_OPEN=8,
        RESEARCH_SHADOW_MAX_OPEN_PER_REGIME=4, RESEARCH_SHADOW_MIN_AGE_MIN=1,
        RESEARCH_SHADOW_MIN_LIQUIDITY_USD=1, RESEARCH_SHADOW_MIN_RANK_SCORE=1))
    assert research.should_open_shadow({'address': 'next', 'entry_regime': 'pump_early',
        'age_minutes': 3., 'liquidity_usd': 1000.}, action='rejected', reason='soft_score',
        proba=.8, threshold=.7, rank_info={'rank_score': 73.})[0]
