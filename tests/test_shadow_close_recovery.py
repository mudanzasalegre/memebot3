"""Synthetic native IO and actual shadow lifecycle; never operational trading."""
import ast
import asyncio
import datetime as dt
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def tick_namespace(tmp_path, writer):
    now = dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc)
    state = {"opened_at": now - dt.timedelta(hours=4), "buy_price_usd": 1.,
        "vec": {"address": "synthetic-shadow", "timestamp": now - dt.timedelta(hours=4)},
        "entry_regime": "pump_early"}
    ns = {"List": list, "SimpleNamespace": SimpleNamespace, "utc_now": lambda: now,
        "PROJECT_ROOT": tmp_path, "_shadow_positions": {"synthetic-shadow": state},
        "_RESEARCH_SHADOW_USE_GECKO": False, "ML_POSITIVE_PNL_RATIO": .05,
        "_seconds_from_opened_at": lambda opened, current: (current - opened).total_seconds(),
        "_to_float": lambda value, *args: float(value) if value is not None else None,
        "exit_policy": SimpleNamespace(effective_exit_policy=lambda sd: SimpleNamespace(max_holding_h=1),
            should_take_partial=lambda *args: False, should_exit=lambda *args, **kw: "TIMEOUT"),
        "price_service": SimpleNamespace(get_price=AsyncMock(return_value={"price_usd": 2.}),
            get_price_usd=AsyncMock(return_value=None)),
        "store_append": writer, "_stats": {"appended_shadow": 0},
        "log": SimpleNamespace(debug=Mock()),
        "research_runtime": SimpleNamespace(record_shadow_close=Mock(), record_shadow_partial=Mock())}
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == "_tick_shadows")
    exec(compile(ast.Module(body=[node], type_ignores=[]), "run_bot.py", "exec"), ns)
    return ns, state


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "error"])
async def test_actual_tick_never_discards_or_counts_unconfirmed_feature_write(tmp_path, failure):
    writer = Mock(return_value=False) if failure == "false" else Mock(side_effect=OSError("synthetic feature IO"))
    ns, state = tick_namespace(tmp_path, writer)
    await ns["_tick_shadows"]()
    assert writer.called
    assert ns["_stats"]["appended_shadow"] == 0
    assert ns["_shadow_positions"]["synthetic-shadow"] is state


def native_paths(root, monkeypatch):
    from features import store
    from analytics import research_runtime as research
    monkeypatch.setattr(store, "DATA_DIR", root / 'features')
    monkeypatch.setattr(research, "METRICS_DIR", root / 'metrics')
    monkeypatch.setattr(research, "RESEARCH_EVENTS_PATH", root / 'metrics/events.jsonl')
    monkeypatch.setattr(research, "RESEARCH_PORTFOLIO_PATH", root / 'portfolio.json')
    monkeypatch.setattr(research, "_OPEN_SHADOWS", {})
    monkeypatch.setattr(research, "append_decision", Mock())
    return store, research


def observation(*, label=1, pnl=100., peak=25000.):
    from runtime.shadow_closes import freeze_observation
    return freeze_observation('synthetic-shadow',
        {'address': 'synthetic-shadow', 'timestamp': dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)},
        closed_at=dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
        close={'regime': 'pump_early', 'pnl_pct': pnl, 'label': label, 'exit_reason': 'TIMEOUT',
            'close_price_usd': 2., 'shadow_kind': 'research', 'extra': {'max_pnl_pct_seen': peak}},
        context={'opened_at': '2026-08-01T00:00:00+00:00', 'reason': 'original'}, instance_id='original')


def artifacts(root, store, research):
    import pandas as pd
    frame = pd.read_parquet(next(store.DATA_DIR.glob('features_*.parquet')))
    rows = [json.loads(line) for line in research.RESEARCH_EVENTS_PATH.read_text(encoding='utf-8').splitlines()]
    return frame, rows


@pytest.mark.parametrize('failure', ['feature_before', 'feature_after', 'event_before', 'event_after', 'portfolio', 'ack'])
def test_native_partial_publication_replays_one_original_feature_and_event(tmp_path, monkeypatch, failure):
    from runtime import shadow_closes as closes
    store, research = native_paths(tmp_path, monkeypatch)
    original = observation()
    with monkeypatch.context() as fault:
        def fail(*args, **kwargs): raise OSError('synthetic publication fault')
        if failure == 'feature_before':
            fault.setattr(store, '_atomic_write_table', fail)
        elif failure == 'feature_after':
            actual = store.append
            def writer(*args, **kwargs):
                actual(*args, **kwargs)
                fail()
            fault.setattr(store, 'append', writer)
        elif failure == 'event_before':
            fault.setattr(research, '_write_event', fail)
        elif failure == 'event_after':
            actual = research._write_event
            def event(*args, **kwargs):
                actual(*args, **kwargs)
                fail()
            fault.setattr(research, '_write_event', event)
        elif failure == 'portfolio':
            fault.setattr(research, '_save_portfolio', fail)
        else:
            actual = closes.os.replace
            def replace(source, destination):
                if Path(source).parent.name == 'pending' and Path(destination).parent.name == 'complete': fail()
                return actual(source, destination)
            fault.setattr(closes.os, 'replace', replace)
        with pytest.raises(OSError): closes.persist_observation(tmp_path, original)
    pending = list((tmp_path / 'data/research/shadow_close_outbox/pending').glob('*.json'))
    assert len(pending) == 1 and json.loads(pending[0].read_text())['observation'] == original
    assert closes.replay_pending(tmp_path) == {'complete': 1, 'failed': 0}
    assert closes.replay_pending(tmp_path) == {'complete': 0, 'failed': 0}
    frame, rows = artifacts(tmp_path, store, research)
    assert len(frame) == len(rows) == 1
    assert frame.outcome_observation_id.tolist() == [original['id']]
    assert rows[0]['outcome_observation_id'] == original['id']
    assert rows[0]['pnl_pct'] == 100. and rows[0]['ts_utc'] == original['closed_at']
    assert frame.timestamp.tolist() == [dt.datetime(2026, 8, 1)]
    assert frame.outcome_closed_at.tolist() == [dt.datetime(2026, 8, 2)]
    assert frame.outcome_trade_id.isna().all() and frame.outcome_return_basis.isna().all()
    assert not pending[0].exists()
    assert len(list((pending[0].parent.parent / 'complete').glob('*.json'))) == 1


@pytest.mark.parametrize('writer_result', [False, None, 1])
def test_no_unconfirmed_feature_result_can_acknowledge_outbox(tmp_path, writer_result):
    from runtime.shadow_closes import persist_observation
    journal = Mock()
    with pytest.raises(OSError, match='unconfirmed'):
        persist_observation(tmp_path, observation(), feature_writer=Mock(return_value=writer_result), journal_writer=journal)
    journal.assert_not_called()
    assert len(list((tmp_path / 'data/research/shadow_close_outbox/pending').glob('*.json'))) == 1


@pytest.mark.asyncio
async def test_actual_tick_retries_frozen_price_time_and_context_without_refetch(tmp_path):
    writer = Mock(side_effect=[False, True])
    ns, state = tick_namespace(tmp_path, writer)
    await ns['_tick_shadows']()
    frozen = deepcopy(state['close_observation'])
    state['vec']['timestamp'] = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
    ns['utc_now'] = lambda: dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
    ns['price_service'].get_price.return_value = {'price_usd': 9.}
    await ns['_tick_shadows']()
    assert ns['price_service'].get_price.await_count == 1
    assert writer.call_args.kwargs['target_total_pnl_pct'] == 100.
    assert writer.call_args.kwargs['outcome_targets']['outcome_closed_at'].isoformat() == frozen['closed_at']
    assert writer.call_args.args[0]['timestamp'].isoformat() == frozen['features']['timestamp']
    assert ns['_stats']['appended_shadow'] == 1 and not ns['_shadow_positions']


@pytest.mark.parametrize('unknown', [None, float('nan'), float('inf')])
def test_native_unknown_result_keeps_peak_without_financial_proof(tmp_path, monkeypatch, unknown):
    from runtime.shadow_closes import persist_observation
    store, research = native_paths(tmp_path, monkeypatch)
    frozen = observation(label=None, pnl=unknown)
    assert persist_observation(tmp_path, frozen)['complete']
    assert persist_observation(tmp_path, frozen)['replayed']
    frame, rows = artifacts(tmp_path, store, research)
    assert frame.label.isna().all() and frame.target_total_pnl_pct.isna().all()
    assert frame.max_pnl_pct_seen.tolist() == [25000.]
    assert rows[0]['label'] is None and rows[0]['pnl_pct'] is None
    assert frame.outcome_execution_proof.isna().all()


def test_completed_identity_rejects_conflicting_result_without_rewriting(tmp_path, monkeypatch):
    from runtime.shadow_closes import persist_observation
    store, research = native_paths(tmp_path, monkeypatch)
    original = observation()
    persist_observation(tmp_path, original)
    paths = [*store.DATA_DIR.glob('*.parquet'), research.RESEARCH_EVENTS_PATH, research.RESEARCH_PORTFOLIO_PATH]
    before = {file: file.read_bytes() for file in paths}
    changed = deepcopy(original)
    changed['close']['pnl_pct'] = 999.
    with pytest.raises(ValueError, match='Conflicting frozen'):
        persist_observation(tmp_path, changed)
    assert all(file.read_bytes() == value for file, value in before.items())


@pytest.mark.parametrize('identity', ['../outside', 'shadow_nothex', None])
def test_invalid_identity_cannot_create_any_outbox_files(tmp_path, identity):
    from runtime.shadow_closes import persist_observation
    frozen = observation()
    frozen['id'] = identity
    with pytest.raises(ValueError): persist_observation(tmp_path, frozen)
    assert not list(tmp_path.iterdir())


def test_concurrent_native_publishers_produce_one_observation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from runtime.shadow_closes import persist_observation
    store, research = native_paths(tmp_path, monkeypatch)
    frozen = observation()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: persist_observation(tmp_path, frozen), range(2)))
    assert all(row['complete'] for row in results)
    assert sum(row['replayed'] for row in results) == 1
    frame, rows = artifacts(tmp_path, store, research)
    assert len(frame) == len(rows) == 1


def test_native_restart_replays_pending_original_outcome_without_open_context(tmp_path, monkeypatch):
    from runtime.shadow_closes import persist_observation
    store, research = native_paths(tmp_path, monkeypatch)
    frozen = observation()
    with monkeypatch.context() as fault:
        fault.setattr(research, '_save_portfolio', Mock(side_effect=OSError('synthetic interrupted portfolio')))
        with pytest.raises(OSError): persist_observation(tmp_path, frozen)
    code = '''
import json, sys
from pathlib import Path
from features import store
from analytics import research_runtime as research
from runtime.shadow_closes import replay_pending
root=Path(sys.argv[1])
store.DATA_DIR=root/'features'
research.METRICS_DIR=root/'metrics'
research.RESEARCH_EVENTS_PATH=root/'metrics/events.jsonl'
research.RESEARCH_PORTFOLIO_PATH=root/'portfolio.json'
assert not research._OPEN_SHADOWS
print(json.dumps(replay_pending(root)))
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
        capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {'complete': 1, 'failed': 0}
    frame, rows = artifacts(tmp_path, store, research)
    assert len(frame) == len(rows) == 1
    assert rows[0]['ts_utc'] == frozen['closed_at']


@pytest.mark.asyncio
async def test_opening_io_cannot_race_a_shadow_close(tmp_path):
    ns, state = tick_namespace(tmp_path, Mock(return_value=True))
    state['opening_pending'] = True
    await ns['_tick_shadows']()
    assert ns['price_service'].get_price.await_count == 0
    assert ns['_shadow_positions']['synthetic-shadow'] is state
    state.pop('opening_pending')
    await ns['_tick_shadows']()
    assert not ns['_shadow_positions']


@pytest.mark.asyncio
async def test_close_availability_clock_follows_actual_lookup_completion(tmp_path):
    ns, state = tick_namespace(tmp_path, Mock(return_value=True))
    clock = [dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc)]
    ns['utc_now'] = lambda: clock[0]
    async def delayed(*args, **kwargs):
        clock[0] += dt.timedelta(minutes=3)
        await asyncio.sleep(0)
        return {'price_usd': 2.}
    ns['price_service'].get_price = AsyncMock(side_effect=delayed)
    await ns['_tick_shadows']()
    assert ns['store_append'].call_args.kwargs['outcome_targets']['outcome_closed_at'] == clock[0]
    assert state['close_observation']['closed_at'] == clock[0].isoformat()


@pytest.mark.parametrize('memory_retained', [True, False])
def test_recovered_old_close_cannot_close_new_opening(tmp_path, monkeypatch, memory_retained):
    from runtime.shadow_closes import persist_observation
    store, research = native_paths(tmp_path, monkeypatch)
    newer = {'shadow_instance_id': 'newer', 'opened_at': '2026-08-03T00:00:00Z', 'reason': 'newer'}
    research.record_shadow_open('synthetic-shadow', payload=newer, shadow_kind='research')
    if not memory_retained: research._OPEN_SHADOWS.clear()
    persist_observation(tmp_path, observation())
    portfolio = json.loads(research.RESEARCH_PORTFOLIO_PATH.read_text())
    assert portfolio['synthetic-shadow']['closed'] is False
    if memory_retained:
        assert research._OPEN_SHADOWS['synthetic-shadow']['shadow_instance_id'] == 'newer'
    else:
        assert not research._OPEN_SHADOWS


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_native_io_then_retries_same_observation(tmp_path):
    started, release = threading.Event(), threading.Event()
    def writer(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return True
    ns, state = tick_namespace(tmp_path, Mock(side_effect=writer))
    task = asyncio.create_task(ns['_tick_shadows']())
    try:
        for _ in range(200):
            if started.is_set(): break
            await asyncio.sleep(.005)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        assert ns['_stats']['appended_shadow'] == 0
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert ns['_shadow_positions']['synthetic-shadow'] is state
    await ns['_tick_shadows']()
    assert not ns['_shadow_positions'] and ns['_stats']['appended_shadow'] == 1
    assert ns['store_append'].call_count == 1 and ns['price_service'].get_price.await_count == 1


def test_shadow_monitor_has_one_supervised_owner_and_is_not_in_entry_loop():
    tree = ast.parse(Path('run_bot.py').read_text(encoding='utf-8'))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
    assert not any(isinstance(node, ast.Name) and node.id == '_tick_shadows'
                   for node in ast.walk(functions['main_loop']))
    assert sum(isinstance(node, ast.Name) and node.id == '_shadow_monitor_loop'
               for node in ast.walk(functions['_runner'])) == 1
    assert sum(isinstance(node, ast.Name) and node.id == '_tick_shadows'
               for node in ast.walk(functions['_shadow_monitor_loop'])) == 1


@pytest.mark.asyncio
async def test_actual_shadow_monitor_waits_for_bootstrap_and_does_not_block_entry(tmp_path):
    ready, received, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def tick():
        received.set()
        await release.wait()
    namespace = {'asyncio': asyncio, 'PROJECT_ROOT': tmp_path, 'SLEEP_SECONDS': 3,
        '_tick_shadows': tick, '_note_runtime_error': lambda *args: pytest.fail(str(args)),
        'log': SimpleNamespace(debug=Mock(), warning=Mock())}
    node = next(item for item in ast.parse(Path('run_bot.py').read_text(encoding='utf-8')).body
                if isinstance(item, ast.AsyncFunctionDef) and item.name == '_shadow_monitor_loop')
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    monitor = asyncio.create_task(namespace['_shadow_monitor_loop'](ready))
    try:
        await asyncio.sleep(.01)
        assert not received.is_set()
        ready.set()
        await asyncio.wait_for(received.wait(), 2)
        entry_progress = []
        async def entry(): entry_progress.append(True)
        await asyncio.wait_for(entry(), .5)
        assert entry_progress and not monitor.done()
    finally:
        monitor.cancel()
        with pytest.raises(asyncio.CancelledError): await monitor
