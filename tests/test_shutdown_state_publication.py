"""Native runner + native local-stop composition, no operator DB or providers.

The startup services are inert boundary stubs. Real supervised ownership,
managed writers, original _runner cleanup, original state publisher and the
local stop protocol execute together; this is not a whole first-launch proof.
"""
from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from runtime.local_stop import LocalStopRequested, StopBinding, run_with_local_stop


def native_shutdown(tmp_path, monkeypatch, *, paper, stage=None, kind=None, hold=False):
    from analytics import research_runtime as research
    from runtime import social_enrichment_queue
    from research_loop import entry_gate_forward
    from fetcher import jupiter_price
    from runtime import trade_learning

    monkeypatch.setattr(research, '_CANDIDATE_WRITER', None)
    ready, publishing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    failure = (asyncio.CancelledError('synthetic internal publication cancellation') if kind == 'cancelled'
        else OSError('synthetic final state unavailable')) if stage else None

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    async def forever(*args, **kwargs): await asyncio.Event().wait()
    async def main(*, positions_ready, hot_queue_enabled):
        positions_ready.set()
        ready.set()
        await forever()
    def closer(name):
        async def close(): calls.append(name)
        return close

    monkeypatch.setattr(social_enrichment_queue, 'stop_background_tasks', closer('social'))
    monkeypatch.setattr(entry_gate_forward, 'stop_background_tasks', closer('research'))
    monkeypatch.setattr(jupiter_price, 'aclose', closer('price_http'))
    monkeypatch.setattr(trade_learning, 'run_export_loop', forever)

    async def snapshot():
        calls.append('snapshot')
        assert namespace['_runtime_process_state'] == 'stopped'
        if stage == 'snapshot': raise failure
        return SimpleNamespace(process_state='stopped')
    async def publish(factory, row):
        assert factory is Session and row.process_state == 'stopped'
        assert set(calls) == {'pump', 'social', 'research', 'price_http', 'snapshot'}
        calls.append('database')
        publishing.set()
        if hold: await release.wait()
        if stage == 'database': raise failure
        calls.append('database_committed')

    namespace = {'asyncio':asyncio, 'CFG':SimpleNamespace(HOT_QUEUE_ENABLED=True, ML_RETRAIN_IN_MAIN_LOOP=False),
        'PROJECT_ROOT':tmp_path, 'DRY_RUN':paper, 'async_init_db':AsyncMock(), 'SessionLocal':Session,
        '_recover_buy_persistence_outbox':AsyncMock(), '_recover_close_persistence_outbox':AsyncMock(),
        '_refresh_green_live_risk':AsyncMock(), '_repair_paper_archive_evidence':AsyncMock(),
        'store_append':Mock(side_effect=AssertionError('Unexpected feature write')),
        '_policy_feature_write_result':Mock(), 'main_loop':main, '_dex_discovery_loop':forever,
        '_pump_discovery_loop':forever, '_position_monitor_loop':forever, '_shadow_monitor_loop':forever,
        '_periodic_labeler':forever, '_open_shadow_now':AsyncMock(return_value=True),
        'control_command_loop':forever, 'runtime_state_loop':forever, '_background_tasks':set(),
        'pumpfun':SimpleNamespace(stop_background_tasks=closer('pump')), '_stats':{},
        '_note_runtime_error':Mock(), '_build_runtime_state_snapshot':snapshot,
        'publish_runtime_state':publish, 'log':SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())}
    source = Path(os.environ.get('MEMEBOT_SHUTDOWN_SOURCE_ROOT', str(Path(__file__).resolve().parents[1])))
    tree = ast.parse((source/'run_bot.py').read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
        and node.name in {'_runner', '_publish_runtime_state_once'}]
    assert len(nodes) == 2
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    binding = StopBinding.create(tmp_path)

    async def launched():
        try:
            await run_with_local_stop(namespace['_runner'], binding)
        except LocalStopRequested:
            # The original native __main__ acknowledges ONLY this boundary.
            binding.complete()
            return 'acknowledged'
    return SimpleNamespace(namespace=namespace, ready=ready, publishing=publishing, release=release,
        calls=calls, failure=failure, binding=binding, launched=launched, research=research)


async def request_stop(probe):
    task = asyncio.create_task(probe.launched())
    await asyncio.wait_for(probe.ready.wait(), 3)
    probe.binding.request()
    return task


def assert_drained(probe):
    from runtime.owned_dispatch import pending_dispatch_count
    assert probe.namespace['_POLICY_FEATURE_WRITER'] is None
    assert probe.namespace['_SHADOW_OPEN_QUEUE'] is None
    assert probe.research._CANDIDATE_WRITER is None
    assert pending_dispatch_count() == 0
    assert {'pump', 'social', 'research', 'price_http'}.issubset(probe.calls)
    probe.namespace['store_append'].assert_not_called()
    probe.namespace['_open_shadow_now'].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('paper', [False, True])
async def test_native_stop_ack_requires_final_database_publication(tmp_path, monkeypatch, paper):
    probe = native_shutdown(tmp_path, monkeypatch, paper=paper)
    task = await request_stop(probe)
    assert await asyncio.wait_for(task, 5) == 'acknowledged'
    assert probe.binding.completed() and probe.calls[-1] == 'database_committed'
    assert_drained(probe)


@pytest.mark.asyncio
@pytest.mark.parametrize('refuse_commit', [False, True])
async def test_native_stop_actual_sqlite_commit_or_failure_controls_acknowledgement(tmp_path, monkeypatch, refuse_commit):
    import datetime as dt
    from dataclasses import replace
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from db.models import BotRuntimeState
    from runtime.state_models import RuntimeStateSnapshot
    from runtime.state_publisher import publish_runtime_state

    database = tmp_path/'owned-runtime-state.db'
    engine = create_async_engine('sqlite+aiosqlite:///' + database.as_posix())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = dt.datetime.now(dt.timezone.utc)
    running = RuntimeStateSnapshot(bot_id='synthetic', updated_at=now, heartbeat_at=now,
        started_at=now, process_state='running', dry_run=True, discovery_paused=False,
        buys_paused=False, retrain_state='idle', reports_refresh_state='idle', wallet_sol=None,
        wallet_checked_at=None, open_positions_count=0, queue_pending=0, queue_requeued=0,
        queue_cooldown=0, queue_oldest_first_seen_at=None, buy_limiter_in_window=0,
        buy_limiter_window_s=60, discovery_last_ok_at=None, monitor_last_ok_at=None,
        last_error=None, last_error_at=None)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(BotRuntimeState.__table__.create)
        await publish_runtime_state(factory, running)
        if refuse_commit:
            # The REAL owned SQLite database refuses the update. No publisher,
            # commit, error check, row or completion receipt is substituted.
            async with engine.begin() as connection:
                await connection.execute(text("CREATE TRIGGER refuse_final_state BEFORE UPDATE OF process_state "
                    "ON bot_runtime_state WHEN NEW.process_state = 'stopped' BEGIN "
                    "SELECT RAISE(ABORT, 'synthetic final state rejection'); END"))
        probe = native_shutdown(tmp_path, monkeypatch, paper=True)
        probe.namespace.update(SessionLocal=factory, publish_runtime_state=publish_runtime_state,
            _build_runtime_state_snapshot=AsyncMock(return_value=replace(running, process_state='stopped')))
        task = await request_stop(probe)
        if refuse_commit:
            with pytest.raises(IntegrityError): await asyncio.wait_for(task, 5)
            assert not probe.binding.completed()
        else:
            assert await asyncio.wait_for(task, 5) == 'acknowledged'
            assert probe.binding.completed()
        async with factory() as session:
            actual = await session.get(BotRuntimeState, 'synthetic')
            assert actual.process_state == ('running' if refuse_commit else 'stopped')
        assert_drained(probe)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('paper', [False, True])
@pytest.mark.parametrize('stage', ['snapshot', 'database'])
@pytest.mark.parametrize('kind', ['error', 'cancelled'])
async def test_native_stop_never_acknowledges_failed_or_cancelled_final_publication(tmp_path, monkeypatch, paper, stage, kind):
    probe = native_shutdown(tmp_path, monkeypatch, paper=paper, stage=stage, kind=kind)
    task = await request_stop(probe)
    expected = RuntimeError if kind == 'cancelled' else OSError
    with pytest.raises(expected) as caught:
        await asyncio.wait_for(task, 5)
    if kind == 'cancelled': assert caught.value.__cause__ is probe.failure
    else: assert caught.value is probe.failure
    assert not probe.binding.completed() and not probe.binding.completion_path().exists()
    assert probe.binding.requested()
    assert 'database_committed' not in probe.calls
    assert_drained(probe)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False, True])
async def test_native_stop_waits_for_final_state_commit_before_acknowledgement(tmp_path, monkeypatch, failure):
    probe = native_shutdown(tmp_path, monkeypatch, paper=True, stage='database' if failure else None, hold=True)
    task = await request_stop(probe)
    await asyncio.wait_for(probe.publishing.wait(), 3)
    for _ in range(4): await asyncio.sleep(0)
    assert not task.done() and not probe.binding.completed()
    probe.release.set()
    if failure:
        with pytest.raises(OSError) as caught: await asyncio.wait_for(task, 5)
        assert caught.value is probe.failure and not probe.binding.completed()
    else:
        assert await asyncio.wait_for(task, 5) == 'acknowledged'
        assert probe.binding.completed() and probe.calls[-1] == 'database_committed'
    assert_drained(probe)
