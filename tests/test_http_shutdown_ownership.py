"""Real aiohttp ownership and native runner cleanup, without provider/operator IO."""
from __future__ import annotations

import ast
import asyncio
from collections import defaultdict
import datetime as dt
import logging
import io
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from fetcher import jupiter_price as price
from utils.logger import HourlySplitFileHandler

ROOT=Path(__file__).resolve().parents[1]
RUNNER_SOURCE=ROOT/'run_bot.py'


@pytest_asyncio.fixture
async def local_http():
    from aiohttp import web
    app=web.Application()
    async def reply(request): return web.Response(text='owned-loopback-response')
    app.router.add_get('/',reply)
    owner=web.AppRunner(app)
    await owner.setup()
    site=web.TCPSite(owner,'127.0.0.1',0)
    await site.start()
    port=site._server.sockets[0].getsockname()[1]
    try: yield f'http://127.0.0.1:{port}/'
    finally: await owner.cleanup()


@pytest.fixture
def native(monkeypatch,tmp_path):
    from runtime import owned_dispatch, social_enrichment_queue
    from research_loop import entry_gate_forward
    monkeypatch.setattr(owned_dispatch,'open_dispatch',Mock())
    closers={name:AsyncMock() for name in ('social','research','dispatch','pump')}
    monkeypatch.setattr(owned_dispatch,'drain_dispatch',closers['dispatch'])
    monkeypatch.setattr(social_enrichment_queue,'stop_background_tasks',closers['social'])
    monkeypatch.setattr(entry_gate_forward,'stop_background_tasks',closers['research'])
    # Compile the native owner without importing the operational run_bot
    # entrypoint or calling its DB, wallet, operator-data or fitting paths.
    tree=ast.parse(RUNNER_SOURCE.read_text(encoding='utf-8'))
    runner=next(node for node in tree.body if isinstance(node,ast.AsyncFunctionDef) and node.name=='_runner')
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
    async def forever(*args,**kwargs): await asyncio.Event().wait()
    published=[]
    async def publish(): published.append(price._SESSION is None or price._SESSION.closed)
    namespace={'asyncio':asyncio,'CFG':SimpleNamespace(ML_RETRAIN_IN_MAIN_LOOP=False,HOT_QUEUE_ENABLED=False),
        'DRY_RUN':True,'PROJECT_ROOT':tmp_path,'SessionLocal':Session,'async_init_db':AsyncMock(),
        '_recover_buy_persistence_outbox':AsyncMock(),'_recover_close_persistence_outbox':AsyncMock(),
        '_repair_paper_archive_evidence':AsyncMock(),'_policy_feature_write_result':Mock(),'store_append':Mock(),
        '_open_shadow_now':AsyncMock(),
        '_note_runtime_error':Mock(),'log':Mock(),'_stats':defaultdict(int),'_background_tasks':set(),
        '_runtime_process_state':'stopped','_POLICY_FEATURE_WRITER':None,'_SHADOW_OPEN_QUEUE':None,
        'main_loop':forever,'_dex_discovery_loop':forever,'_position_monitor_loop':forever,
        '_shadow_monitor_loop':forever,'_periodic_labeler':forever,'control_command_loop':forever,
        'runtime_state_loop':forever,'_publish_runtime_state_once':publish,
        'pumpfun':SimpleNamespace(stop_background_tasks=closers['pump'])}
    from runtime import trade_learning
    monkeypatch.setattr(trade_learning,'run_export_loop',forever)
    exec(compile(ast.Module(body=[runner],type_ignores=[]),'run_bot.py','exec'),namespace)
    monkeypatch.setattr(price,'_SESSION',None)
    return namespace,closers,published


@pytest.mark.asyncio
async def test_actual_runner_closes_real_price_session_even_when_startup_fails(native):
    namespace,closers,published=native
    sessions=[]
    async def fail_start():
        sessions.append(await price._ensure_session())
        raise LookupError('controlled startup failure')
    namespace['async_init_db']=fail_start
    try:
        with pytest.raises(LookupError,match='controlled startup failure'):
            await namespace['_runner']()
        assert sessions[0].closed and price._SESSION is None
        assert published==[True]
        assert all(close.await_count==1 for close in closers.values())
    finally:
        for session in sessions: await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_owner',['pump','social','research','dispatch'])
async def test_cleanup_failure_drains_every_other_owner_and_withholds_stopped(native,failed_owner):
    namespace,closers,published=native
    sessions=[]
    async def fail_start():
        sessions.append(await price._ensure_session())
        raise LookupError('controlled startup failure')
    namespace['async_init_db']=fail_start
    closers[failed_owner].side_effect=RuntimeError('controlled '+failed_owner+' cleanup failure')
    try:
        with pytest.raises(RuntimeError,match='controlled '+failed_owner+' cleanup failure'):
            await namespace['_runner']()
        assert sessions[0].closed and price._SESSION is None
        assert all(close.await_count==1 for close in closers.values())
        assert not published and namespace['_runtime_process_state']!='stopped'
    finally:
        for session in sessions: await session.close()


@pytest.mark.asyncio
async def test_actual_running_owner_waits_for_real_http_close_despite_repeated_cancel(native,monkeypatch,local_http):
    namespace,closers,published=native
    entered,closing,release=(asyncio.Event() for _ in range(3))
    sessions=[]
    async def main(*,positions_ready,**kwargs):
        session=await price._ensure_session()
        sessions.append(session)
        connector=session.connector
        async with session.get(local_http) as response:
            assert response.status==200 and await response.text()=='owned-loopback-response'
        assert connector is not None and connector._conns and not connector.closed
        original=session.close
        async def held_close():
            closing.set()
            await release.wait()
            await original()
            assert connector.closed
        monkeypatch.setattr(session,'close',held_close)
        positions_ready.set()
        entered.set()
        await asyncio.Event().wait()
    namespace['main_loop']=main
    task=asyncio.create_task(namespace['_runner']())
    try:
        await asyncio.wait_for(entered.wait(),5)
        task.cancel()
        await asyncio.wait_for(closing.wait(),5)
        task.cancel(); await asyncio.sleep(0)
        task.cancel(); await asyncio.sleep(0)
        assert not task.done() and not published and not sessions[0].closed
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert sessions[0].closed and price._SESSION is None and published==[True]
    finally:
        release.set()
        if not task.done(): task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        for session in sessions: await session.close()


@pytest.mark.asyncio
async def test_price_close_retains_unconfirmed_resource_for_retry(monkeypatch):
    session=SimpleNamespace(closed=False,close=AsyncMock())
    monkeypatch.setattr(price,'_SESSION',session)
    with pytest.raises(RuntimeError,match='closed'):
        await price.aclose()
    assert price._SESSION is session


@pytest.mark.asyncio
async def test_price_close_cannot_clear_an_interleaved_replacement(monkeypatch):
    replacement=SimpleNamespace(closed=False,close=AsyncMock())
    session=SimpleNamespace(closed=False)
    async def close():
        session.closed=True
        monkeypatch.setattr(price,'_SESSION',replacement)
    session.close=close
    monkeypatch.setattr(price,'_SESSION',session)
    with pytest.raises(RuntimeError,match='changed'):
        await price.aclose()
    assert price._SESSION is replacement


@pytest.mark.asyncio
async def test_price_close_detaches_already_closed_reference_without_reopening(monkeypatch):
    session=SimpleNamespace(closed=True,close=AsyncMock())
    monkeypatch.setattr(price,'_SESSION',session)
    await price.aclose()
    assert price._SESSION is None and not session.close.await_count
    await price.aclose()


@pytest.mark.parametrize('rotate',[False,True])
def test_closed_file_handler_does_not_write_or_reopen_on_late_diagnostic(tmp_path,rotate):
    handler=HourlySplitFileHandler(tmp_path,1)
    record=logging.LogRecord('asyncio',logging.ERROR,__file__,1,'late diagnostic',(),None)
    handler.close()
    if rotate: handler._period_start-=dt.timedelta(hours=2)
    handler._open_file=Mock(side_effect=AssertionError('Closed logger must not reopen'))
    handler.handle(record)
    assert handler._file.closed and not handler._open_file.call_count
    handler.close()


def test_active_file_handler_does_not_silence_an_actual_io_failure(tmp_path):
    handler=HourlySplitFileHandler(tmp_path,1)
    handler._file.close()  # External corruption, not an owned handler shutdown.
    record=logging.LogRecord('test',logging.ERROR,__file__,1,'must fail visibly',(),None)
    try:
        with pytest.raises(ValueError,match='closed file'): handler.handle(record)
    finally: handler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['raises','unconfirmed'])
async def test_runner_withholds_stopped_when_http_cleanup_is_unconfirmed(native,monkeypatch,failure):
    namespace,closers,published=native
    sessions=[]
    real_close=[]
    async def fail_start():
        session=await price._ensure_session()
        sessions.append(session)
        real_close.append(session.close)
        async def bad_close():
            if failure=='raises': raise RuntimeError('controlled HTTP close failure')
        monkeypatch.setattr(session,'close',bad_close)
        raise LookupError('controlled startup failure')
    namespace['async_init_db']=fail_start
    try:
        with pytest.raises(RuntimeError,match='HTTP|closed'): await namespace['_runner']()
        assert not published and namespace['_runtime_process_state']!='stopped'
        assert price._SESSION is sessions[0] and not sessions[0].closed
        assert all(close.await_count==1 for close in closers.values())
    finally:
        for close in real_close: await close()


@pytest.mark.asyncio
async def test_native_cleanup_does_not_allocate_an_unused_http_client(native,monkeypatch):
    namespace,closers,published=native
    namespace['async_init_db'].side_effect=LookupError('controlled startup failure')
    forbidden=Mock(side_effect=AssertionError('Shutdown must not create an HTTP client'))
    monkeypatch.setattr(price.aiohttp,'ClientSession',forbidden)
    with pytest.raises(LookupError): await namespace['_runner']()
    assert price._SESSION is None and not forbidden.call_count and published==[True]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',[OSError,asyncio.CancelledError])
async def test_price_close_propagates_original_failure_and_preserves_owner(monkeypatch,kind):
    error=kind('controlled close IO failure')
    session=SimpleNamespace(closed=False,close=AsyncMock(side_effect=error))
    monkeypatch.setattr(price,'_SESSION',session)
    with pytest.raises(kind) as captured: await price.aclose()
    assert captured.value is error and price._SESSION is session


def test_file_close_serializes_with_an_inflight_handler(tmp_path):
    handler=HourlySplitFileHandler(tmp_path,1)
    started,done=threading.Event(),threading.Event()
    def close():
        started.set()
        try: handler.close()
        finally: done.set()
    handler.acquire()
    worker=threading.Thread(target=close)
    worker.start()
    try:
        assert started.wait(2)
        assert not done.wait(.1),'File close bypassed the active handler lock'
        assert not handler._file.closed
    finally:
        handler.release()
        worker.join(2)
        handler.close()
    assert done.is_set() and not worker.is_alive() and handler._file.closed


def test_active_file_handler_retains_messages_and_hourly_rotation(tmp_path):
    handler=HourlySplitFileHandler(tmp_path,1)
    try:
        first=handler._file
        handler.handle(logging.LogRecord('test',logging.INFO,__file__,1,'first message',(),None))
        handler._period_start-=dt.timedelta(hours=2)
        handler.handle(logging.LogRecord('test',logging.INFO,__file__,1,'second message',(),None))
        assert first.closed and handler._file is not first and not handler._file.closed
        text=''.join(path.read_text(encoding='utf-8') for path in tmp_path.glob('*.txt'))
        assert text.count('first message')==text.count('second message')==1
    finally: handler.close()


def test_closed_file_sink_does_not_hide_late_diagnostic_from_live_console(tmp_path):
    handler=HourlySplitFileHandler(tmp_path,1)
    console=io.StringIO()
    live=logging.StreamHandler(console)
    logger=logging.Logger('isolated-late-cleanup-diagnostic')
    logger.addHandler(handler)
    logger.addHandler(live)
    handler.close()
    try:
        logger.error('unclosed resource must remain visible')
        assert console.getvalue().strip()=='unclosed resource must remain visible'
        assert handler._file.closed
    finally:
        live.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_owner',['pump','social','research','dispatch','http'])
async def test_local_stop_cannot_acknowledge_an_internally_cancelled_resource_cleanup(native,monkeypatch,tmp_path,failed_owner):
    from runtime.local_stop import StopBinding,run_with_local_stop
    namespace,closers,published=native
    binding=StopBinding.create(tmp_path)
    error=asyncio.CancelledError('controlled internal cleanup cancellation')
    sessions,real_close=[],[]
    async def main(*,positions_ready,**kwargs):
        session=await price._ensure_session()
        sessions.append(session)
        real_close.append(session.close)
        if failed_owner=='http':
            monkeypatch.setattr(session,'close',AsyncMock(side_effect=error))
        positions_ready.set()
        binding.request()
        await asyncio.Event().wait()
    namespace['main_loop']=main
    if failed_owner!='http': closers[failed_owner].side_effect=error
    try:
        with pytest.raises(RuntimeError,match='cleanup was cancelled') as captured:
            await run_with_local_stop(namespace['_runner'],binding)
        assert captured.value.__cause__ is error
        assert not published and namespace['_runtime_process_state']!='stopped'
        assert not binding.completed()
        assert all(close.await_count==1 for close in closers.values())
        if failed_owner=='http':
            assert not sessions[0].closed and price._SESSION is sessions[0]
        else:
            assert sessions[0].closed and price._SESSION is None
    finally:
        for close in real_close: await close()


@pytest.mark.asyncio
async def test_local_stop_surfaces_internal_closed_writer_cancellation_instead_of_ack(native,monkeypatch,tmp_path):
    from runtime.local_stop import StopBinding,run_with_local_stop
    from features.policy_write_queue import PolicyWriteQueue
    namespace,closers,published=native
    binding=StopBinding.create(tmp_path)
    error=asyncio.CancelledError('controlled closed-writer cancellation')
    original=PolicyWriteQueue.close
    async def close(writer):
        await original(writer)
        assert writer.snapshot()['closed']
        raise error
    monkeypatch.setattr(PolicyWriteQueue,'close',close)
    async def main(*,positions_ready,**kwargs):
        positions_ready.set()
        binding.request()
        await asyncio.Event().wait()
    namespace['main_loop']=main
    with pytest.raises(RuntimeError,match='cleanup was cancelled') as captured:
        await run_with_local_stop(namespace['_runner'],binding)
    assert captured.value.__cause__ is error and not binding.completed()
    assert published==[True] and namespace['_POLICY_FEATURE_WRITER'] is None
