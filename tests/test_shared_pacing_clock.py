"""Native monotonic admission boundary; no provider or operational start."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace

import pytest

from fetcher import geckoterminal as gt
from utils import rate_limiter as rate


class LockSpy:
    def __init__(self):
        self.lock = threading.RLock()
        self.local = threading.local()
    def __enter__(self):
        self.lock.acquire()
        self.local.held = True
        return self
    def __exit__(self, *args):
        self.local.held = False
        self.lock.release()
    def held_here(self): return getattr(self.local, 'held', False)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(gt, '_state_lock', threading.RLock())
    monkeypatch.setattr(gt, '_last_call_ts', 0.)
    monkeypatch.setattr(gt, '_last_observed_ts', None)
    monkeypatch.setattr(gt, '_min_interval_s', .008)
    def forbidden(*args, **kwargs): raise AssertionError('Real provider HTTP forbidden')
    monkeypatch.setattr(gt.requests, 'get', forbidden)
    if gt.aiohttp is not None: monkeypatch.setattr(gt.aiohttp, 'ClientSession', forbidden)


def engine(monkeypatch, target, clock, lock=None, interval=1.):
    if target == 'gecko':
        monkeypatch.setattr(gt, 'time', SimpleNamespace(monotonic=clock, sleep=time.sleep))
        monkeypatch.setattr(gt, '_min_interval_s', interval)
        if lock is not None: monkeypatch.setattr(gt, '_state_lock', lock)
        return gt._throttle_wait, lambda: (gt._last_call_ts, gt._last_observed_ts), gt._throttle_async, gt._throttle_internal
    limiter = rate.RateLimiter(1, interval)
    limiter._last_reset = limiter._last_observed = 0.
    monkeypatch.setattr(rate, 'time', SimpleNamespace(monotonic=clock, sleep=time.sleep))
    if lock is not None: limiter._lock = lock
    return limiter._permission_or_wait, lambda: (limiter._tokens, limiter._last_reset, limiter._last_observed), limiter.acquire, limiter.acquire_sync


@pytest.mark.parametrize('target', ['gecko', 'fixed_window'])
def test_native_clock_is_sampled_inside_the_admission_lock(monkeypatch, target):
    lock = LockSpy()
    def clock():
        assert lock.held_here(), 'A pre-lock sample can invent a reversal under thread scheduling'
        return 100.
    permission, _, _, _ = engine(monkeypatch, target, clock, lock)
    assert permission() == 0.
    assert permission() == 1.


@pytest.mark.parametrize('target', ['gecko', 'fixed_window'])
def test_contended_same_clock_can_only_grant_once(monkeypatch, target):
    lock = LockSpy()
    samples = []
    def clock():
        assert lock.held_here()
        samples.append(100.)
        return 100.
    permission, _, _, _ = engine(monkeypatch, target, clock, lock)
    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(lambda _: permission(), range(64)))
    assert result.count(0.) == 1 and result.count(1.) == 63 and len(samples) == 64


@pytest.mark.parametrize('target', ['gecko', 'fixed_window'])
def test_real_clock_reversal_remains_conservative_and_bounded(monkeypatch, target):
    now = [100.]
    permission, _, _, _ = engine(monkeypatch, target, lambda: now[0])
    assert permission() == 0.
    now[0] = 99.
    assert permission() == 1.
    now[0] = 99.5
    assert permission() == .5
    now[0] = 100.
    assert permission() == 0.
    assert permission() == 1.


@pytest.mark.parametrize('target', ['gecko', 'fixed_window'])
@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf'), None, '100', True, 10**400],
                         ids=['nan', 'inf', 'negative_inf', 'none', 'string', 'bool', 'overflow'])
def test_invalid_clock_fails_closed_without_mutating_admission(monkeypatch, target, bad):
    now = [100.]
    permission, state, _, _ = engine(monkeypatch, target, lambda: now[0])
    permission()
    before = state()
    now[0] = bad
    with pytest.raises(ValueError): permission()
    assert state() == before
    now[0] = 101.
    assert permission() == 0.


@pytest.mark.parametrize('target', ['gecko', 'fixed_window'])
@pytest.mark.asyncio
async def test_cancelled_waiter_reserves_no_future_slot_and_leaves_lock_free(monkeypatch, target):
    now = [100.]
    lock = LockSpy()
    permission, state, acquire, _ = engine(monkeypatch, target, lambda: now[0], lock)
    permission()
    before = state()
    module = gt if target == 'gecko' else rate
    def try_lock():
        with lock: return True
    async def sleep(delay):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(try_lock).result(timeout=2)
        raise asyncio.CancelledError
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError): await acquire()
    assert state() == before
    now[0] = 101.
    assert permission() == 0.


@pytest.mark.parametrize('target', ['gecko', 'fixed_window'])
def test_early_sync_and_async_wakes_retry_shared_permission(monkeypatch, target):
    now = [100.]
    permission, _, acquire_async, acquire_sync = engine(monkeypatch, target, lambda: now[0])
    permission()
    module = gt if target == 'gecko' else rate
    sleeps = []
    def sleep(delay):
        sleeps.append(delay)
        now[0] += .25
    async def async_sleep(delay): sleep(delay)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(sleep=async_sleep))
    acquire_sync()
    asyncio.run(acquire_async())
    assert sleeps == [1., .75, .5, .25] * 2
    assert now[0] == 102. and permission() == 1.


@pytest.mark.parametrize('target', ['gecko', 'fixed_window'])
def test_native_sync_and_async_threads_share_interval(monkeypatch, target):
    lock, grants = LockSpy(), []
    permission, _, acquire_async, acquire_sync = engine(monkeypatch, target, time.monotonic, lock, interval=.008)
    # Capture the admitted clock in the same thread, inside the native lock.
    module = gt if target == 'gecko' else rate
    real_permission = permission
    thread_clock = threading.local()
    def clock():
        assert lock.held_here()
        thread_clock.value = time.monotonic()
        return thread_clock.value
    def record():
        wait = real_permission()
        if wait == 0.: grants.append(thread_clock.value)
        return wait
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=clock, sleep=time.sleep))
    if target == 'gecko': monkeypatch.setattr(gt, '_throttle_wait', record)
    else:
        limiter = acquire_sync.__self__
        monkeypatch.setattr(limiter, '_permission_or_wait', record)
    barrier = threading.Barrier(4)
    async def cycle():
        for _ in range(4): await acquire_async()
    def worker(index):
        barrier.wait(timeout=5)
        if index % 2: asyncio.run(asyncio.wait_for(cycle(), 10))
        else:
            for _ in range(4): acquire_sync()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(worker, i) for i in range(4)]
        for future in futures: future.result(timeout=15)
    # Appending happens after native lock release, so OS scheduling may reorder
    # that bookkeeping. Compare actual native admission clocks, not callback order.
    ordered = sorted(grants)
    assert len(grants) == len(set(grants)) == 16
    assert all(right-left >= .008 for left, right in zip(ordered, ordered[1:]))


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), None, '100', True, 10**400],
                         ids=['nan', 'inf', 'none', 'string', 'bool', 'overflow'])
def test_invalid_initial_clock_cannot_create_an_unbounded_window(monkeypatch, bad):
    monkeypatch.setattr(rate, 'time', SimpleNamespace(monotonic=lambda: bad))
    with pytest.raises(ValueError): rate.RateLimiter(1, 1.)
