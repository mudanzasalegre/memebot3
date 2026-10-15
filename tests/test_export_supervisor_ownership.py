"""Native exporter and supervisor must retain IO ownership through shutdown."""
from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


async def wait_thread(event):
    deadline = asyncio.get_running_loop().time() + 5
    while not event.is_set():
        assert asyncio.get_running_loop().time() < deadline, 'Synthetic thread did not reach its boundary'
        await asyncio.sleep(.001)


@pytest.mark.asyncio
async def test_native_export_owner_cannot_release_thread_after_repeated_cancellation(tmp_path, monkeypatch):
    from runtime import trade_learning as learning
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def blocked(**kwargs):
        started.set()
        try:
            assert release.wait(10)
            return {'status': 'ok', 'failed': 0, 'written': 1}
        finally: finished.set()
    monkeypatch.setattr(learning, 'repair_exports', blocked)
    ready = asyncio.Event(); ready.set()
    results = []
    owner = asyncio.create_task(learning.run_export_loop(ready=ready, root=tmp_path,
        cfg=SimpleNamespace(DRY_RUN=True), on_result=results.append, on_error=results.append))
    try:
        await wait_thread(started)
        owner.cancel()
        await asyncio.sleep(.01)
        owner.cancel()
        await asyncio.sleep(.05)
        assert owner.cancelling() == 2
        assert not owner.done() and not finished.is_set(), 'Exporter abandoned an admitted filesystem thread'
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        await wait_thread(finished)
    assert owner.cancelled() and not results


@pytest.mark.asyncio
async def test_native_supervisor_does_not_cancel_child_cleanup_twice():
    from runtime.loop_scheduler import supervise
    entered, draining, release, finished = (asyncio.Event() for _ in range(4))
    async def child():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
            finished.set()
    owner = asyncio.create_task(supervise([('synthetic-child', child())]))
    try:
        await entered.wait()
        owner.cancel()
        await draining.wait()
        owner.cancel()
        await asyncio.sleep(.01)
        assert not owner.done(), 'Supervisor abandoned child cleanup after a second cancellation'
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
    assert owner.cancelled() and finished.is_set()


@pytest.mark.asyncio
async def test_actual_runner_does_not_publish_stopped_before_export_io_finishes(tmp_path, monkeypatch):
    from runtime import trade_learning as learning, social_enrichment_queue
    from research_loop import entry_gate_forward
    from test_shadow_state_recovery import native_namespace
    from test_shadow_open_queue import function
    namespace, store, research, now = native_namespace(tmp_path, monkeypatch)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    stopping = asyncio.Event()
    published = []
    def blocked(**kwargs):
        started.set()
        try:
            assert release.wait(10)
            (tmp_path/'owned-export-finished.txt').write_text('synthetic completed IO', encoding='utf-8')
            return {'status': 'ok', 'failed': 0, 'written': 1}
        finally: finished.set()
    monkeypatch.setattr(learning, 'repair_exports', blocked)
    monkeypatch.setattr(social_enrichment_queue, 'stop_background_tasks', AsyncMock())
    monkeypatch.setattr(entry_gate_forward, 'stop_background_tasks', AsyncMock())
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    async def forever(*args, **kwargs): await asyncio.Event().wait()
    async def main(*, positions_ready, **kwargs):
        positions_ready.set()
        await forever()
    async def fault():
        await wait_thread(started)
        stopping.set()
        raise RuntimeError('synthetic critical sibling stopped')
    async def stopped():
        published.append(finished.is_set())
    namespace.update(asyncio=asyncio, CFG=SimpleNamespace(DRY_RUN=True,
        ML_RETRAIN_IN_MAIN_LOOP=False, HOT_QUEUE_ENABLED=False), DRY_RUN=True,
        async_init_db=AsyncMock(), SessionLocal=Session,
        _recover_buy_persistence_outbox=AsyncMock(), _recover_close_persistence_outbox=AsyncMock(),
        _repair_paper_archive_evidence=AsyncMock(),
        _refresh_green_live_risk=AsyncMock(), _policy_feature_write_result=lambda *args: None,
        main_loop=main, _dex_discovery_loop=forever, _position_monitor_loop=forever,
        _shadow_monitor_loop=forever, _periodic_labeler=forever, control_command_loop=fault,
        runtime_state_loop=forever, _background_tasks=set(), _publish_runtime_state_once=stopped,
        pumpfun=SimpleNamespace(stop_background_tasks=AsyncMock()))
    exec(compile(ast.Module(body=[function('_runner')], type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    owner = asyncio.create_task(namespace['_runner']())
    try:
        await asyncio.wait_for(stopping.wait(), 2)
        for _ in range(10): await asyncio.sleep(0)
        owner.cancel()
        await asyncio.sleep(.01)
        owner.cancel()
        await asyncio.sleep(.01)
        assert not owner.done() and not published, 'Runtime published stopped while export IO remained live'
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        await wait_thread(finished)
    assert published == [True] and (tmp_path/'owned-export-finished.txt').is_file()


@pytest.mark.asyncio
async def test_export_cancelled_before_readiness_does_not_start_io(tmp_path, monkeypatch):
    from runtime import trade_learning as learning
    calls = []
    monkeypatch.setattr(learning, 'repair_exports', lambda **kwargs: calls.append(kwargs))
    owner = asyncio.create_task(learning.run_export_loop(ready=asyncio.Event(), root=tmp_path,
        cfg=SimpleNamespace(DRY_RUN=True), on_result=calls.append, on_error=calls.append))
    await asyncio.sleep(0)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError): await owner
    assert not calls and not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False, True])
async def test_native_export_reports_once_then_waits_serially(tmp_path, monkeypatch, failure):
    from runtime import trade_learning as learning
    results, errors, calls = [], [], []
    expected = {'status': 'ok', 'attempted': 0, 'failed': 0, 'written': 0}
    error = OSError('synthetic source read failure')
    def work(**kwargs):
        calls.append((kwargs, threading.get_ident()))
        if failure: raise error
        return expected
    monkeypatch.setattr(learning, 'repair_exports', work)
    class End(Exception): pass
    async def sleep(seconds):
        assert seconds == 30 and len(calls) == 1
        raise End()
    ready = asyncio.Event(); ready.set()
    with pytest.raises(End):
        await learning.run_export_loop(ready=ready, root=tmp_path, cfg=SimpleNamespace(DRY_RUN=True),
            on_result=results.append, on_error=errors.append, sleep=sleep)
    assert calls[0][1] != threading.get_ident()
    assert calls[0][0]['force'] is True
    assert results == ([] if failure else [expected]) and errors == ([error] if failure else [])


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False, True])
async def test_repeated_export_cancellation_consumes_late_worker_error_without_callback(tmp_path, monkeypatch, failure):
    from runtime import trade_learning as learning
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    callbacks = []
    def work(**kwargs):
        started.set()
        try:
            assert release.wait(10)
            if failure: raise OSError('synthetic late IO error')
            return {'status': 'ok'}
        finally: finished.set()
    monkeypatch.setattr(learning, 'repair_exports', work)
    ready = asyncio.Event(); ready.set()
    owner = asyncio.create_task(learning.run_export_loop(ready=ready, root=tmp_path,
        cfg=SimpleNamespace(DRY_RUN=True), on_result=callbacks.append, on_error=callbacks.append))
    try:
        await wait_thread(started)
        for _ in range(5):
            owner.cancel()
            await asyncio.sleep(0)
        assert not owner.done()
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        await wait_thread(finished)
    assert owner.cancelled() and not callbacks


@pytest.mark.asyncio
async def test_supervisor_closes_unsubmitted_coroutines_and_drains_created_task_on_start_failure(monkeypatch):
    import inspect
    from runtime.loop_scheduler import supervise
    async def child(): await asyncio.Event().wait()
    coroutines = [child(), child(), child()]
    created = []
    native_create = asyncio.create_task
    error = RuntimeError('synthetic task admission failure')
    def create(coro, *, name=None):
        if name == 'second': raise error
        task = native_create(coro, name=name)
        created.append(task)
        return task
    monkeypatch.setattr(asyncio, 'create_task', create)
    with pytest.raises(RuntimeError) as captured:
        await supervise(list(zip(('first', 'second', 'third'), coroutines)))
    assert captured.value is error and len(created) == 1 and created[0].done()
    assert all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in coroutines)


@pytest.mark.asyncio
async def test_supervisor_producer_failure_closes_collected_work_before_starting_tasks():
    import inspect
    from runtime.loop_scheduler import supervise
    entered = []
    async def child(): entered.append(True)
    coroutines = [child(), child()]
    error = OSError('synthetic coroutine producer failed')
    def source():
        yield ('first', coroutines[0])
        yield ('second', coroutines[1])
        raise error
    with pytest.raises(OSError) as captured: await supervise(source())
    assert captured.value is error and not entered
    assert all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in coroutines)


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [None, ('bad',), ('bad', 1)])
async def test_supervisor_malformed_admission_cannot_orphan_previous_work(bad):
    import inspect
    from runtime.loop_scheduler import supervise
    async def child(): await asyncio.Event().wait()
    original = child()
    with pytest.raises(TypeError): await supervise([('first', original), bad])
    assert inspect.getcoroutinestate(original) == inspect.CORO_CLOSED


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['return', 'error', 'cancel'])
async def test_supervisor_preserves_original_critical_outcome_and_drains_cleanup(mode):
    from runtime.loop_scheduler import supervise
    entered, draining, release, finished = (asyncio.Event() for _ in range(4))
    error = ValueError('original critical error')
    async def sibling():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
            finished.set()
    async def first():
        await entered.wait()
        if mode == 'error': raise error
        if mode == 'cancel': raise asyncio.CancelledError()
    owner = asyncio.create_task(supervise([('critical', first()), ('sibling', sibling())]))
    try:
        await draining.wait()
        assert not owner.done()
    finally:
        release.set()
    if mode == 'error':
        with pytest.raises(ValueError) as captured: await owner
        assert captured.value is error
    elif mode == 'cancel':
        with pytest.raises(asyncio.CancelledError): await owner
    else:
        with pytest.raises(RuntimeError, match='critical'): await owner
    assert finished.is_set()


@pytest.mark.asyncio
async def test_empty_supervisor_does_not_create_background_work():
    from runtime.loop_scheduler import supervise
    before = asyncio.all_tasks()
    await supervise([])
    assert asyncio.all_tasks() == before
