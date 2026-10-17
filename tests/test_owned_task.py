import asyncio
import inspect

import pytest

from runtime.owned_thread import await_owned_task


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', [False, True])
async def test_owned_task_returns_original_value_or_error(failed):
    error = ValueError('synthetic')
    async def work():
        if failed: raise error
        return {'original':True}
    if failed:
        with pytest.raises(ValueError) as caught: await await_owned_task(work())
        assert caught.value is error
    else: assert await await_owned_task(work()) == {'original':True}


@pytest.mark.asyncio
@pytest.mark.parametrize('repeated', [False, True])
@pytest.mark.parametrize('failed', [False, True])
async def test_owned_task_drains_before_propagating_cancellation(repeated, failed):
    started, release = asyncio.Event(), asyncio.Event()
    finished = []
    async def work():
        started.set()
        await release.wait()
        finished.append(True)
        if failed: raise ValueError('synthetic worker failure after stop')
        return 1
    owner = asyncio.create_task(await_owned_task(work()))
    await started.wait()
    owner.cancel()
    for _ in range(4): await asyncio.sleep(0)
    if repeated:
        owner.cancel()
        for _ in range(4): await asyncio.sleep(0)
    assert not owner.done() and not finished
    release.set()
    with pytest.raises(asyncio.CancelledError): await owner
    assert finished == [True]


@pytest.mark.asyncio
async def test_precancelled_owner_does_not_admit_new_work():
    called = []
    async def work(): called.append(True)
    coroutine = work()
    async def owner():
        asyncio.current_task().cancel()
        await await_owned_task(coroutine)
    with pytest.raises(asyncio.CancelledError): await asyncio.create_task(owner())
    assert not called and inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, False, lambda:None])
async def test_invalid_owned_task_cannot_start(value):
    with pytest.raises(TypeError): await await_owned_task(value)


@pytest.mark.asyncio
async def test_owned_task_creation_failure_closes_unsubmitted_coroutine(monkeypatch):
    async def work(): return 1
    coroutine = work()
    def fail(*args, **kwargs): raise RuntimeError('synthetic admission failure')
    monkeypatch.setattr(asyncio, 'create_task', fail)
    with pytest.raises(RuntimeError, match='admission failure'): await await_owned_task(coroutine)
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_internal_worker_cancellation_is_not_a_normal_owner_stop():
    async def work(): raise asyncio.CancelledError('synthetic internal cancellation')
    with pytest.raises(RuntimeError, match='internally') as caught: await await_owned_task(work())
    assert isinstance(caught.value.__cause__, asyncio.CancelledError)
