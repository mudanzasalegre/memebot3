"""Bounded inline sync ownership and native Gecko receipts; no real HTTP."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import asyncio
import threading
import time

import pytest

from fetcher import geckoterminal as gt
from utils import simple_cache
from utils.bounded_state import BoundedFailureCounter
from utils.request_ownership import OwnedRequests, SyncOwnedRequests

MINT = "So11111111111111111111111111111111111111112"
OTHER = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def settled(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, "Synthetic owner did not reach the expected state"
        time.sleep(.001)


def idle(requests):
    state = requests.snapshot()
    assert state["owners"] == state["joiners"] == state["latest_keys"] == 0


def test_inline_factory_uses_callers_thread_and_creates_no_workers(monkeypatch):
    requests = SyncOwnedRequests()
    caller = threading.get_ident()
    def forbidden(*args, **kwargs): raise AssertionError("No detached worker allowed")
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    assert requests.run("one", lambda: threading.get_ident()) == caller
    idle(requests)


def test_compatible_joiners_share_one_owner_and_admission_hook():
    requests = SyncOwnedRequests()
    entered, release = threading.Event(), threading.Event()
    calls, admissions = [], []
    result = {"original": 1}
    def factory(): calls.append(1); entered.set(); assert release.wait(2); return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(requests.run, "one", factory, on_admit=lambda: admissions.append("owner"))
        try:
            assert entered.wait(2)
            joiner = pool.submit(requests.run, "one", factory, on_admit=lambda: admissions.append("joiner"))
            settled(lambda: requests.snapshot()["joiners"] == 1)
        finally: release.set()
        assert owner.result(timeout=2) is joiner.result(timeout=2) is result
    assert calls == [1] and admissions == ["owner"]
    idle(requests)


@pytest.mark.parametrize("force", [False, True])
def test_owner_capacity_rejection_never_calls_admission_or_factory(force):
    requests = SyncOwnedRequests(1, 1)
    entered, release = threading.Event(), threading.Event()
    rejected = []
    def factory(): entered.set(); assert release.wait(2); return 1
    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(requests.run, "one", factory)
        try:
            assert entered.wait(2)
            assert requests.run("one" if force else "two", lambda: rejected.append("factory"),
                force_refresh=force, on_admit=lambda: rejected.append("hook")) is None
            assert rejected == [] and requests.snapshot()["owners"] == 1
        finally: release.set()
        assert owner.result(timeout=2) == 1
    idle(requests)


def test_joiner_limit_rejection_preserves_active_owner_and_existing_joiner():
    requests = SyncOwnedRequests(1, 1)
    entered, release = threading.Event(), threading.Event()
    def factory(): entered.set(); assert release.wait(2); return 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(requests.run, "one", factory)
        try:
            assert entered.wait(2)
            joiner = pool.submit(requests.run, "one", factory)
            settled(lambda: requests.snapshot()["joiners"] == 1)
            assert requests.run("one", lambda: pytest.fail("No third factory")) is None
            assert not owner.done() and not joiner.done()
        finally: release.set()
        assert owner.result(timeout=2) == joiner.result(timeout=2) == 1
    idle(requests)


def test_join_timeout_releases_only_joiner_and_never_cancels_owner():
    requests = SyncOwnedRequests()
    entered, release = threading.Event(), threading.Event()
    def factory(): entered.set(); assert release.wait(2); return 1
    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(requests.run, "one", factory)
        try:
            assert entered.wait(2)
            assert requests.run("one", factory, join_timeout=.015) is None
            assert not owner.done()
            assert requests.snapshot()["owners"] == 1 and requests.snapshot()["joiners"] == 0
            assert requests.snapshot()["join_timeouts"] == 1
        finally: release.set()
        assert owner.result(timeout=2) == 1
    idle(requests)


@pytest.mark.parametrize("kind", [ValueError, TimeoutError, KeyboardInterrupt])
def test_original_factory_exception_propagates_to_owner_and_joiner(kind):
    requests = SyncOwnedRequests()
    entered, release = threading.Event(), threading.Event()
    error = kind("Synthetic owner failure")
    def factory(): entered.set(); assert release.wait(2); raise error
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(requests.run, "one", factory)
        try:
            assert entered.wait(2)
            joiner = pool.submit(requests.run, "one", factory)
            settled(lambda: requests.snapshot()["joiners"] == 1)
        finally: release.set()
        for future in (owner, joiner):
            with pytest.raises(kind) as caught: future.result(timeout=2)
            assert caught.value is error
    assert requests.snapshot()["join_timeouts"] == 0
    idle(requests)


def test_failed_admission_allocates_no_ownership_and_no_factory():
    requests = SyncOwnedRequests()
    def fail(): raise RuntimeError("Synthetic admission failure")
    with pytest.raises(RuntimeError, match="admission failure"):
        requests.run("one", lambda: pytest.fail("No factory"), on_admit=fail)
    idle(requests)
    assert requests.run("one", lambda: 1) == 1
    idle(requests)


def test_recursive_same_thread_join_rejects_without_waiting_or_cancelling():
    requests = SyncOwnedRequests()
    nested = []
    def factory():
        nested.append(requests.run("one", lambda: pytest.fail("No recursive factory")))
        return 1
    assert requests.run("one", factory) == 1
    assert nested == [None] and requests.snapshot()["capacity_rejections"] == 1
    idle(requests)


@pytest.mark.parametrize("value", [None, True, False, 0, -1, float("nan"), float("inf"),
    float("-inf"), {}, [], "invalid", 10**400],
    ids=["none","true","false","zero","negative","nan","inf","negative_inf","dict","list","string","overflow"])
def test_invalid_join_timeout_allocates_nothing(value):
    requests = SyncOwnedRequests()
    with pytest.raises(ValueError, match="join timeout"):
        requests.run("one", lambda: pytest.fail("No invalid-timeout factory"), join_timeout=value)
    idle(requests)


def test_forced_owner_becomes_latest_without_old_completion_removing_it():
    requests = SyncOwnedRequests(2)
    first_entered, first_release = threading.Event(), threading.Event()
    new_entered, new_release = threading.Event(), threading.Event()
    def first(): first_entered.set(); assert first_release.wait(2); return "old"
    def newer(): new_entered.set(); assert new_release.wait(2); return "new"
    with ThreadPoolExecutor(max_workers=3) as pool:
        old = pool.submit(requests.run, "one", first)
        try:
            assert first_entered.wait(2)
            new = pool.submit(requests.run, "one", newer, force_refresh=True)
            assert new_entered.wait(2)
            joiner = pool.submit(requests.run, "one", lambda: pytest.fail("Join latest owner"))
            settled(lambda: requests.snapshot()["joiners"] == 1)
            first_release.set()
            assert old.result(timeout=2) == "old"
            assert requests.snapshot()["latest_keys"] == 1 and not joiner.done()
        finally:
            first_release.set(); new_release.set()
        assert new.result(timeout=2) == joiner.result(timeout=2) == "new"
    idle(requests)


def token_payload(address=MINT, price=2):
    return {"data": {"id": f"solana_{address}", "type": "token",
        "attributes": {"address": address, "price_usd": str(price), "market_cap_usd": None,
            "fdv_usd": "1000", "total_reserve_in_usd": "5000", "volume_usd": {"h24": "100"}}}}


class SyncResponse:
    status_code = 200
    headers = {}
    def __init__(self, address=MINT, price=2): self.body, self.closed = token_payload(address, price), 0
    def json(self): return deepcopy(self.body)
    def close(self): self.closed += 1


class SyncClient:
    def __init__(self, address=MINT, price=2, block=False):
        self.address, self.response, self.block = address, SyncResponse(address, price), block
        self.lock = threading.Lock()
        self.calls, self.closed = [], 0
        self.entered, self.release = threading.Event(), threading.Event()
    def get(self, url, **kwargs):
        assert url == f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{self.address}"
        assert kwargs["allow_redirects"] is False and kwargs["params"] == {"include": "top_pools"}
        with self.lock: self.calls.append((url, kwargs))
        self.entered.set()
        if self.block: assert self.release.wait(2)
        return self.response
    def close(self): self.closed += 1


@pytest.fixture
def native(monkeypatch):
    assert Path(gt.__file__).resolve().is_relative_to(Path.cwd().resolve())
    simple_cache.cache_clear()
    monkeypatch.setattr(gt, "_SYNC_REQUESTS", SyncOwnedRequests(name="gecko-sync-native-test"))
    monkeypatch.setattr(gt, "_REQUESTS", OwnedRequests(name="gecko-async-native-test"))
    monkeypatch.setattr(gt, "_fail_count", BoundedFailureCounter())
    monkeypatch.setattr(gt, "_state_lock", threading.RLock())
    monkeypatch.setattr(gt, "_latest_request", {})
    monkeypatch.setattr(gt, "_active_requests", 0)
    monkeypatch.setattr(gt, "USE_GECKO_TERMINAL", True)
    monkeypatch.setattr(gt, "_min_interval_s", 0.)
    monkeypatch.setattr(gt, "_last_call_ts", 0.)
    monkeypatch.setattr(gt, "_last_observed_ts", None)
    monkeypatch.setattr(gt, "_cooldown_until", 0.)
    class Limiter:
        max_calls = 10
        def acquire_sync(self): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(gt, "GECKO_LIMITER", Limiter())
    def forbidden(*args, **kwargs): raise AssertionError("Real HTTP forbidden")
    monkeypatch.setattr(gt.requests, "get", forbidden)
    monkeypatch.setattr(gt.requests, "Session", forbidden)
    if gt.aiohttp is not None: monkeypatch.setattr(gt.aiohttp, "ClientSession", forbidden)
    yield
    state = gt.request_runtime_snapshot()
    assert state["active_requests"] == state["latest_generations"] == 0
    idle(gt._SYNC_REQUESTS)
    assert state["requests"]["owners"] == state["requests"]["joiners"] == 0
    simple_cache.cache_clear()


def test_native_compatible_threads_share_one_original_detached_receipt(native):
    client = SyncClient(block=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(gt.get_token_data, "solana", MINT, client)
        try:
            assert client.entered.wait(2)
            joiner = pool.submit(gt.get_token_data, "solana", MINT, client)
            settled(lambda: gt._SYNC_REQUESTS.snapshot()["joiners"] == 1)
            assert gt._active_requests == 1 and len(client.calls) == 1
        finally: client.release.set()
        rows = [owner.result(timeout=2), joiner.result(timeout=2)]
    assert rows[0] == rows[1] and rows[0] is not rows[1]
    rows[0]["price_usd"] = 99
    assert rows[1]["price_usd"] == 2 and client.response.closed == 1 and client.closed == 0
    assert gt.get_token_data("solana", MINT, client)["market_observation"] == rows[1]["market_observation"]
    assert len(client.calls) == 1


@pytest.mark.parametrize("difference", ["session", "timeout", "force"])
def test_native_incompatible_or_forced_callers_do_not_share(native, difference):
    first_client = SyncClient(block=True)
    second_client = SyncClient(block=True) if difference == "session" else first_client
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(gt.get_token_data, "solana", MINT, first_client)
        try:
            assert first_client.entered.wait(2)
            second = pool.submit(gt.get_token_data, "solana", MINT, second_client,
                timeout=4 if difference == "timeout" else 5, force_refresh=difference == "force")
            settled(lambda: len(first_client.calls) + (len(second_client.calls) if second_client is not first_client else 0) == 2)
            assert gt._active_requests == 2
        finally:
            first_client.release.set(); second_client.release.set()
        assert first.result(timeout=2)["price_usd"] == second.result(timeout=2)["price_usd"] == 2


@pytest.mark.parametrize("capacity", ["owned", "shared"])
def test_native_rejected_force_preserves_existing_valid_receipt(native, monkeypatch, capacity):
    client = SyncClient()
    previous = gt.get_token_data("solana", MINT, client, force_refresh=True)
    leases, pool, owner = [], None, None
    if capacity == "shared":
        leases = [(f"occupied-{i}", gt._begin_request(f"occupied-{i}")) for i in range(gt._MAX_ACTIVE)]
    else:
        monkeypatch.setattr(gt._SYNC_REQUESTS, "max_owners", 1)
        occupied = SyncClient(OTHER, block=True)
        pool = ThreadPoolExecutor(max_workers=1)
        owner = pool.submit(gt.get_token_data, "solana", OTHER, occupied)
        assert occupied.entered.wait(2)
    try:
        assert gt.get_token_data("solana", MINT, client, force_refresh=True) is None
        found, hit = gt._cached(f"gt:{gt._CONTRACT}:solana:{MINT}", MINT)
        assert found and hit["market_observation"] == previous["market_observation"]
        assert len(client.calls) == 1
    finally:
        for key, generation in leases: gt._end_request(key, generation)
        if pool is not None:
            occupied.release.set()
            owner.result(timeout=2)
            pool.shutdown()


@pytest.mark.parametrize("older_failed", [False, True])
def test_native_delayed_older_sync_owner_cannot_replace_newer_force(native, monkeypatch, older_failed):
    old_client, fresh_client = SyncClient(price=1), SyncClient(price=2)
    if older_failed: old_client.response.status_code = 503
    held, release = threading.Event(), threading.Event()
    original_fetch = gt._fetch_sync
    def delayed(network, address, key, session, timeout, generation):
        if session is old_client:
            held.set()
            assert release.wait(2)
        return original_fetch(network, address, key, session, timeout, generation)
    monkeypatch.setattr(gt, "_fetch_sync", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(gt.get_token_data, "solana", MINT, old_client)
        try:
            assert held.wait(2)
            fresh = gt.get_token_data("solana", MINT, fresh_client, force_refresh=True)
        finally: release.set()
        previous = owner.result(timeout=2)
    assert previous is None if older_failed else previous["price_usd"] == 1
    cached = gt.get_token_data("solana", MINT, fresh_client)
    assert cached["price_usd"] == 2 and cached["market_observation"] == fresh["market_observation"]
    assert gt._fail_count.get(f"gt:{gt._CONTRACT}:solana:{MINT}", 0) == 0


def test_native_join_timeout_keeps_owner_and_original_http_timeout(native):
    client = SyncClient(block=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(gt.get_token_data, "solana", MINT, client, timeout=.015)
        try:
            assert client.entered.wait(2)
            assert gt.get_token_data("solana", MINT, client, timeout=.015) is None
            assert not owner.done() and gt._active_requests == 1
            assert len(client.calls) == 1 and client.calls[0][1]["timeout"] == .015
        finally: client.release.set()
        assert owner.result(timeout=2)["price_usd"] == 2
    assert client.response.closed == 1


def test_native_owned_session_is_closed_once_after_shared_factory(native, monkeypatch):
    client = SyncClient(block=True)
    monkeypatch.setattr(gt.requests, "Session", lambda: client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(gt.get_token_data, "solana", MINT)
        try:
            assert client.entered.wait(2)
            joiner = pool.submit(gt.get_token_data, "solana", MINT)
            settled(lambda: gt._SYNC_REQUESTS.snapshot()["joiners"] == 1)
        finally: client.release.set()
        assert owner.result(timeout=2)["price_usd"] == joiner.result(timeout=2)["price_usd"] == 2
    assert client.closed == client.response.closed == len(client.calls) == 1


def test_native_cleanup_failure_does_not_share_an_unchecked_success(native):
    client = SyncClient()
    def fail(): raise RuntimeError("Synthetic response cleanup failure")
    client.response.close = fail
    assert gt.get_token_data("solana", MINT, client, force_refresh=True) is None
    assert gt.get_token_data("solana", MINT, client) is None
    assert len(client.calls) == 1


def test_native_cache_invalidation_error_allocates_no_sync_lease(native, monkeypatch):
    def fail(key): raise RuntimeError("Synthetic cache invalidation failure")
    monkeypatch.setattr(gt, "cache_delete", fail)
    client = SyncClient()
    with pytest.raises(RuntimeError, match="cache invalidation"):
        gt.get_token_data("solana", MINT, client, force_refresh=True)
    assert not client.calls


@pytest.mark.parametrize("older_mode", ["sync", "async"])
def test_native_sync_async_modes_share_original_latest_admission(native, monkeypatch, older_mode):
    class AsyncResponse:
        status = 200
        headers = {}
        def __init__(self, price): self.body = token_payload(price=price)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return deepcopy(self.body)
    class AsyncClient:
        def __init__(self, price): self.price = price
        def get(self, url, **kwargs): return AsyncResponse(self.price)
    old_client, fresh_sync = SyncClient(price=1), SyncClient(price=2)
    fresh_async = AsyncClient(1 if older_mode == "async" else 2)
    held, release = threading.Event(), threading.Event()
    if older_mode == "sync":
        original = gt._fetch_sync
        def fetch(*args):
            held.set()
            assert release.wait(2)
            return original(*args)
        monkeypatch.setattr(gt, "_fetch_sync", fetch)
        old_call = lambda: gt.get_token_data("solana", MINT, old_client)
        fresh_call = lambda: asyncio.run(gt.get_token_data_async("solana", MINT, fresh_async, force_refresh=True))
    else:
        original = gt._fetch_async
        async def fetch(network, address, key, session, timeout, generation):
            if session is fresh_async:
                held.set()
                assert release.wait(2)
            return await original(network, address, key, session, timeout, generation)
        monkeypatch.setattr(gt, "_fetch_async", fetch)
        old_call = lambda: asyncio.run(gt.get_token_data_async("solana", MINT, fresh_async))
        fresh_call = lambda: gt.get_token_data("solana", MINT, fresh_sync, force_refresh=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(old_call)
        try:
            assert held.wait(2)
            fresh = fresh_call()
        finally: release.set()
        owner.result(timeout=2)
    found, cached = gt._cached(f"gt:{gt._CONTRACT}:solana:{MINT}", MINT)
    assert found and cached["price_usd"] == 2 and cached["market_observation"] == fresh["market_observation"]
