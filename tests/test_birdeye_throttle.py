"""Native global pacing across loops/threads with synthetic HTTP only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from fetcher import birdeye as be
from utils import simple_cache as cache
from utils.provider_access import ResourceAccess
from utils.request_ownership import OwnedRequests

MINT = 'So11111111111111111111111111111111111111112'
OTHER = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    # Reset the native lock using its actual type: the regression must not
    # replace the original async lock with the proposed implementation.
    monkeypatch.setattr(be, '_lock', type(be._lock)())
    monkeypatch.setattr(be, '_last_call_ts', 0.)
    monkeypatch.setattr(be, '_last_observed_ts', None, raising=False)
    monkeypatch.setattr(be, '_MIN_INTERVAL', .008)
    monkeypatch.setattr(be, '_API_KEY', 'synthetic-pacing-key-not-real')
    monkeypatch.setattr(be, '_RESOURCE_ACCESS', ResourceAccess((be._TOKEN_EP, be._POOL_EP, be._CREATION_EP)))
    monkeypatch.setattr(be, '_REQUESTS', OwnedRequests(16, 64, name='synthetic-pacing-request'))
    monkeypatch.setattr(be, '_latest_request', {})
    cache._CACHE.clear()
    be._fail_count.clear()
    def forbidden(*args, **kwargs):
        raise AssertionError('Real HTTP forbidden')
    monkeypatch.setattr(be.aiohttp, 'ClientSession', forbidden)
    yield
    assert be._REQUESTS.snapshot()['owners'] == be._REQUESTS.snapshot()['joiners'] == 0
    assert not be._latest_request
    cache._CACHE.clear()
    be._fail_count.clear()


async def cycle(count=3):
    async def call():
        await be._throttle()
        return be._last_call_ts
    return await asyncio.wait_for(asyncio.gather(*(call() for _ in range(count))), 10)


def assert_spaced(stamps):
    ordered = sorted(stamps)
    assert len(set(ordered)) == len(ordered)
    assert all(right - left >= be._MIN_INTERVAL for left, right in zip(ordered, ordered[1:]))


def capture_admissions(monkeypatch):
    """Capture the native accepted clock under its actual reentrant lock.

    Session setup/caller scheduling is downstream and may observe later or
    newer shared state. This adds no timing tolerance or future reservation.
    """
    admitted=[]
    original=be._throttle_wait
    def wait():
        with be._lock:
            delay=original()
            if delay==0:
                admitted.append(be._last_call_ts)
            return delay
    monkeypatch.setattr(be,'_throttle_wait',wait)
    return admitted


def test_reuses_one_limiter_across_different_event_loops():
    stamps = asyncio.run(cycle()) + asyncio.run(cycle())
    assert_spaced(stamps)


def test_global_spacing_across_four_threads_and_event_loops(monkeypatch):
    # Record clock reads inside the native admission lock, not downstream
    # scheduling timestamps that could move after an OS thread is paused.
    reads = []
    admissions = capture_admissions(monkeypatch)
    def clock():
        now = time.monotonic()
        reads.append(now)
        return now
    monkeypatch.setattr(be, 'time', SimpleNamespace(monotonic=clock, time=time.time))
    barrier = threading.Barrier(4)
    def run():
        barrier.wait(timeout=5)
        return asyncio.run(cycle(4))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [future.result(timeout=15) for future in [pool.submit(run) for _ in range(4)]]
    # Every admitted value comes from the shared native monotonic clock.
    assert all(value in reads for result in results for value in result)
    assert len(admissions) == sum(len(result) for result in results) == 16
    assert_spaced(admissions)
    assert reads == sorted(reads)


def native_clock(monkeypatch, value):
    monkeypatch.setattr(be, 'time', SimpleNamespace(monotonic=lambda: value[0], time=time.time))


def test_monotonic_clock_is_sampled_inside_the_lock(monkeypatch):
    class Lock:
        held = False
        def __enter__(self): self.held = True
        def __exit__(self, *args): self.held = False
    lock = Lock()
    def clock():
        assert lock.held, 'Sampling before admission can invent a clock reversal under contention'
        return 100.
    monkeypatch.setattr(be, '_lock', lock)
    monkeypatch.setattr(be, 'time', SimpleNamespace(monotonic=clock))
    assert be._throttle_wait() == 0
    assert be._last_call_ts == 100.


@pytest.mark.asyncio
async def test_early_wakes_retry_permission_instead_of_sending_early(monkeypatch):
    now = [100.]
    native_clock(monkeypatch, now)
    monkeypatch.setattr(be, '_MIN_INTERVAL', 1.)
    assert be._throttle_wait() == 0
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
        now[0] += .25
    monkeypatch.setattr(be, 'asyncio', SimpleNamespace(sleep=sleep))
    await be._throttle()
    assert sleeps == [1., .75, .5, .25]
    assert be._last_call_ts == 101.


@pytest.mark.asyncio
async def test_oversleep_uses_actual_admission_not_a_reserved_future_slot(monkeypatch):
    now = [100.]
    native_clock(monkeypatch, now)
    monkeypatch.setattr(be, '_MIN_INTERVAL', 1.)
    be._throttle_wait()
    async def sleep(delay): now[0] += 4.
    monkeypatch.setattr(be, 'asyncio', SimpleNamespace(sleep=sleep))
    await be._throttle()
    assert be._last_call_ts == 104.
    assert be._throttle_wait() == 1.


def test_clock_reversal_waits_one_full_interval_and_recovers(monkeypatch):
    now = [100.]
    native_clock(monkeypatch, now)
    monkeypatch.setattr(be, '_MIN_INTERVAL', 1.)
    assert be._throttle_wait() == 0
    now[0] = 99.
    assert be._throttle_wait() == 1.
    now[0] = 99.5
    assert be._throttle_wait() == .5
    now[0] = 100.
    assert be._throttle_wait() == 0
    assert be._last_call_ts == 100.


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf'), None, '100', True])
def test_invalid_clock_fails_closed_without_mutating_last_admission(monkeypatch, bad):
    now = [100.]
    native_clock(monkeypatch, now)
    be._throttle_wait()
    before = (be._last_call_ts, be._last_observed_ts)
    now[0] = bad
    with pytest.raises(ValueError): be._throttle_wait()
    assert (be._last_call_ts, be._last_observed_ts) == before
    now[0] = 101.
    assert be._throttle_wait() == 0


@pytest.mark.parametrize('bad', [0., -1., float('nan'), float('inf'), '1'])
def test_corrupt_interval_fails_closed(monkeypatch, bad):
    monkeypatch.setattr(be, '_MIN_INTERVAL', bad)
    with pytest.raises(ValueError): be._throttle_wait()
    assert be._last_call_ts == 0.


def test_cancelled_waiter_neither_reserves_a_slot_nor_binds_the_next_loop(monkeypatch):
    monkeypatch.setattr(be, '_MIN_INTERVAL', .04)
    async def cancel_cycle():
        await be._throttle()
        admitted = be._last_call_ts
        tasks = [asyncio.create_task(be._throttle()) for _ in range(3)]
        await asyncio.sleep(0)
        assert be._last_call_ts == admitted
        for task in tasks: task.cancel()
        result = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(value, asyncio.CancelledError) for value in result)
        assert be._last_call_ts == admitted
        return admitted
    stamp = asyncio.run(cancel_cycle())
    assert_spaced([stamp, *asyncio.run(cycle())])


@pytest.mark.asyncio
async def test_wait_never_holds_native_lock_across_await(monkeypatch):
    now = [100.]
    native_clock(monkeypatch, now)
    monkeypatch.setattr(be, '_MIN_INTERVAL', 1.)
    be._throttle_wait()
    admitted = be._last_call_ts
    def try_lock():
        acquired = be._lock.acquire(timeout=.5)
        if acquired: be._lock.release()
        return acquired
    async def sleep(delay):
        with ThreadPoolExecutor(max_workers=1) as worker:
            assert worker.submit(try_lock).result(timeout=2)
        raise asyncio.CancelledError
    monkeypatch.setattr(be, 'asyncio', SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError): await be._throttle()
    assert be._last_call_ts == admitted


class Response:
    def __init__(self, status=200, data=None, gate=None):
        self.status, self.data, self.gate = status, data, gate
    async def __aenter__(self):
        if self.gate is not None: await self.gate.wait()
        return self
    async def __aexit__(self, *args): pass
    async def json(self): return {'success': True, 'data': deepcopy(self.data)}


def install(monkeypatch, response):
    calls = []
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            calls.append((time.monotonic(), url, kwargs))
            return response(len(calls), url, kwargs['params']['address'])
    monkeypatch.setattr(be.aiohttp, 'ClientSession', lambda *a, **k: Session())
    return calls


def market(address, price=2):
    return {'address': address, 'price': price, 'liquidity': 10000, 'priceChange5mPercent': 25000}


async def settle():
    for _ in range(20): await asyncio.sleep(0)


def test_native_http_shared_pacing_survives_new_event_loops(monkeypatch):
    admissions = capture_admissions(monkeypatch)
    calls = install(monkeypatch, lambda n, u, a: Response(data=market(a)))
    async def fetch():
        return await asyncio.gather(be.get_token_info(MINT, force_refresh=True),
                                    be.get_token_info(OTHER, force_refresh=True))
    for _ in range(2):
        assert all(row['price_pct_5m'] == 25000 for row in asyncio.run(fetch()))
    assert len(calls) == 4
    assert len(admissions) == len(calls)
    assert_spaced(admissions)


@pytest.mark.asyncio
async def test_three_resources_share_one_global_admission(monkeypatch):
    admissions = capture_admissions(monkeypatch)
    def response(n, url, address):
        if url.endswith(be._CREATION_EP):
            return Response(data={'tokenAddress': address, 'slot': 42, 'txHash': '1'*64,
                                  'blockUnixTime': int(time.time())-180})
        return Response(data=market(address))
    calls = install(monkeypatch, response)
    rows = await asyncio.gather(be._fetch(be._TOKEN_EP, 'pacing:token', address=MINT),
                               be._fetch(be._POOL_EP, 'pacing:pool', address=OTHER),
                               be.get_token_creation_info(MINT))
    assert all(rows) and len(calls) == 3
    assert len(admissions) == len(calls)
    assert_spaced(admissions)


@pytest.mark.asyncio
async def test_same_request_coalesces_without_consuming_extra_pacing_slot(monkeypatch):
    gate = asyncio.Event()
    calls = install(monkeypatch, lambda n, u, a: Response(data=market(a), gate=gate))
    tasks = [asyncio.create_task(be.get_token_info(MINT)) for _ in range(6)]
    await settle()
    assert len(calls) == 1
    gate.set()
    assert all(await asyncio.gather(*tasks))
    assert be._REQUESTS.snapshot()['owners'] == 0
    assert await be.get_token_info(MINT)  # cache hit does not reserve a slot
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancelled_native_request_wait_releases_ownership(monkeypatch):
    monkeypatch.setattr(be, '_MIN_INTERVAL', .1)
    admissions = capture_admissions(monkeypatch)
    calls = install(monkeypatch, lambda n, u, a: Response(data=market(a)))
    assert await be.get_token_info(MINT)
    admitted = be._last_call_ts
    task = asyncio.create_task(be.get_token_info(OTHER, force_refresh=True))
    await settle()
    assert be._REQUESTS.snapshot()['owners'] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert len(calls) == 1 and be._last_call_ts == admitted
    assert be._REQUESTS.snapshot()['owners'] == 0 and not be._latest_request
    assert len(admissions) == 1
    assert await be.get_token_info(OTHER, force_refresh=True)
    assert len(calls) == 2
    assert len(admissions) == len(calls)
    assert_spaced(admissions)


@pytest.mark.asyncio
async def test_waiting_native_request_does_not_send_after_resource_denial(monkeypatch):
    monkeypatch.setattr(be, '_MIN_INTERVAL', .05)
    gate = asyncio.Event()
    calls = install(monkeypatch, lambda n, u, a: Response(403, gate=gate))
    first = asyncio.create_task(be.get_token_creation_info(MINT))
    await settle()
    second = asyncio.create_task(be.get_token_creation_info(OTHER, force_refresh=True))
    await settle()
    assert len(calls) == 1 and be._REQUESTS.snapshot()['owners'] == 2
    gate.set()
    assert await first is None and await second is None
    assert len(calls) == 1 and be.creation_resource_status()['status'] == 'access_denied'


def test_older_completed_denial_blocks_newer_admitted_but_unsent_work():
    now = [100.]
    access = ResourceAccess((be._CREATION_EP,), 5, clock=lambda: now[0])
    old = access.admit(be._CREATION_EP, 'synthetic-context')
    waiting = access.admit(be._CREATION_EP, 'synthetic-context')
    assert access.finish(old, 401)
    assert not access.wire_allowed(waiting, 'synthetic-context')
    assert access.admit(be._CREATION_EP, 'synthetic-context') is None
    now[0] = 105.
    probe = access.admit(be._CREATION_EP, 'synthetic-context')
    assert probe.recovery_probe and access.wire_allowed(probe, 'synthetic-context')
    assert access.admit(be._CREATION_EP, 'synthetic-context') is None
    access.release(probe)


def test_late_older_success_cannot_replace_newer_completed_denial():
    access = ResourceAccess((be._CREATION_EP,))
    old = access.admit(be._CREATION_EP, 'synthetic-context')
    newest = access.admit(be._CREATION_EP, 'synthetic-context')
    assert access.finish(newest, 403)
    assert not access.finish(old, 200, valid_response=True)
    assert access.status(be._CREATION_EP, 'synthetic-context')['status'] == 'access_denied'


def test_newer_completed_valid_response_supersedes_older_completed_denial():
    access = ResourceAccess((be._CREATION_EP,))
    old = access.admit(be._CREATION_EP, 'synthetic-context')
    already_sent = access.admit(be._CREATION_EP, 'synthetic-context')
    assert access.finish(old, 401)
    assert access.finish(already_sent, 200, valid_response=True)
    assert not access.finish(old, 403)
    assert access.status(be._CREATION_EP, 'synthetic-context')['status'] == 'available'


@pytest.mark.asyncio
async def test_rotation_during_native_wait_sends_no_old_credential(monkeypatch):
    monkeypatch.setattr(be, '_MIN_INTERVAL', .05)
    calls = install(monkeypatch, lambda n, u, a: Response(data=market(a)))
    await be.get_token_info(MINT)
    task = asyncio.create_task(be.get_token_info(OTHER, force_refresh=True))
    await settle()
    be._API_KEY = 'synthetic-second-context-not-real'
    assert await task is None
    assert len(calls) == 1 and be._REQUESTS.snapshot()['owners'] == 0


@pytest.mark.asyncio
async def test_force_refresh_keeps_newest_cache_with_native_pacing(monkeypatch):
    gate = asyncio.Event()
    calls = install(monkeypatch, lambda n, u, a: Response(data=market(a, 2 if n == 1 else 7),
                                                        gate=gate if n == 1 else None))
    old = asyncio.create_task(be.get_token_info(MINT))
    await settle()
    newest = await be.get_token_info(MINT, force_refresh=True)
    gate.set()
    assert (await old)['price_usd'] == 2 and newest['price_usd'] == 7
    assert (await be.get_token_info(MINT))['price_usd'] == 7
    assert len(calls) == 2
    assert_spaced([row[0] for row in calls])


@pytest.mark.asyncio
async def test_invalid_pacing_clock_never_sends_http_and_drains_ownership(monkeypatch):
    monkeypatch.setattr(be, 'time', SimpleNamespace(monotonic=lambda: float('nan'), time=time.time))
    calls = install(monkeypatch, lambda n, u, a: Response(data=market(a)))
    assert await be.get_token_info(MINT, force_refresh=True) is None
    assert not calls and be._REQUESTS.snapshot()['owners'] == 0 and not be._latest_request


@pytest.mark.parametrize('rpm', ['invalid', '0', '-1', '60', '600', '9'*400],
                         ids=['malformed', 'zero', 'negative', 'default', 'fast', 'overflow'])
def test_optional_rpm_cold_import_is_safe_and_interval_positive(rpm, monkeypatch):
    monkeypatch.setenv('BIRDEYE_RPM', rpm)
    monkeypatch.setenv('BIRDEYE_API_KEY', 'synthetic-cold-import-key-not-real')
    root = Path(be.__file__).resolve().parents[1]
    code = ('import json,math,os,sys; import config.config; from pathlib import Path; '
            'assert "fetcher.birdeye" not in sys.modules; '
            'os.environ["BIRDEYE_RPM"]=sys.argv[1]; '
            'os.environ["BIRDEYE_API_KEY"]="synthetic-cold-import-key-not-real"; '
            'from fetcher import birdeye as b; '
            'assert os.environ["BIRDEYE_RPM"]==sys.argv[1]; '
            'assert Path(b.__file__).resolve().is_relative_to(Path.cwd().resolve()); '
            'print(json.dumps({"positive":math.isfinite(b._MIN_INTERVAL) and b._MIN_INTERVAL>0,"interval":b._MIN_INTERVAL}))')
    result = subprocess.run([sys.executable, '-c', code, rpm], cwd=root, capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    expected = {'invalid': 1., '0': 60., '-1': 60., '60': 1., '600': .1}.get(rpm, 1.)
    assert json.loads(result.stdout.strip()) == {'positive': True, 'interval': expected}
