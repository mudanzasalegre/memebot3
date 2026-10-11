"""Actual native Dex request/cache ownership with synthetic HTTP only."""
import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from fetcher import dexscreener as dex
from utils import simple_cache as cache
from utils.request_ownership import OwnedRequests

MINT = "So11111111111111111111111111111111111111112"
OTHER = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def payload(price=1, mint=MINT):
    return [{"chainId": "solana", "baseToken": {"address": mint, "symbol": "SYNTHETIC"},
        "pairAddress": mint, "dexId": "pumpswap", "priceUsd": str(price),
        "liquidity": {"usd": 50000}, "volume": {"h24": 1000000},
        "marketCap": 100000, "fdv": 150000, "priceChange": {"m5": 1000000}}]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    cache.cache_clear()
    dex._fail_count.clear()
    monkeypatch.setattr(dex, "_latest_request", {}, raising=False)
    if hasattr(dex, "_REQUESTS"):
        monkeypatch.setattr(dex, "_REQUESTS", OwnedRequests(4, 16, name="synthetic-dex"))
    clients = []
    lock = threading.Lock()
    class Client:
        def __init__(self):
            with lock:
                self.index = len(clients)
                clients.append(self)
            self.closed = 0
        async def __aenter__(self): return self
        async def __aexit__(self, *args): self.closed += 1
    monkeypatch.setattr(dex.aiohttp, "ClientSession", Client)
    async def forbidden(*args, **kwargs): raise AssertionError("Unstubbed HTTP forbidden")
    monkeypatch.setattr(dex, "_fetch_json", forbidden)
    yield clients
    requests = getattr(dex, "_REQUESTS", None)
    if requests is not None:
        state = requests.snapshot()
        assert state["owners"] == state["joiners"] == state["loop_buckets"] == 0
    assert not dex._latest_request
    cache.cache_clear()
    dex._fail_count.clear()


async def settle():
    for _ in range(40): await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_normal_callers_coalesce_original_receipt_and_detach_results(monkeypatch, isolated):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def fetch(url, client, **kwargs):
        calls.append(client.index)
        started.set()
        await release.wait()
        return payload()
    monkeypatch.setattr(dex, "_fetch_json", fetch)
    tasks = [asyncio.create_task(dex.get_pair(MINT)) for _ in range(8)]
    await asyncio.wait_for(started.wait(), 1)
    await settle()
    before = len(calls)
    release.set()
    rows = await asyncio.gather(*tasks)
    assert before == 1
    assert len(isolated) == 1 and isolated[0].closed == 1
    assert all(row["price_pct_5m"] == 1000000 for row in rows)
    proof = deepcopy(rows[1]["market_observation"])
    assert all(row["market_observation"] == proof for row in rows)
    rows[0]["liquidity"]["usd"] = -1
    rows[0]["market_observation"]["fields"].clear()
    cached = await dex.get_pair(MINT)
    assert cached["market_observation"] == proof and cached["liquidity_usd"] == 50000
    assert rows[1]["market_observation"] == proof


@pytest.mark.asyncio
@pytest.mark.parametrize("old_failed", [False, True])
async def test_older_outcome_cannot_replace_newer_forced_cache(monkeypatch, old_failed):
    started, release = asyncio.Event(), asyncio.Event()
    async def fetch(url, client, **kwargs):
        if client.index == 0:
            started.set()
            await release.wait()
            return None if old_failed else payload(1)
        return payload(2)
    monkeypatch.setattr(dex, "_fetch_json", fetch)
    older = asyncio.create_task(dex.get_pair(MINT))
    await asyncio.wait_for(started.wait(), 1)
    fresh = await dex.get_pair(MINT, force_refresh=True)
    release.set()
    old = await older
    assert (old is None) if old_failed else old["price_usd"] == 1
    cached = await dex.get_pair(MINT)
    assert cached is not None and cached["price_usd"] == 2
    assert cached["market_observation"] == fresh["market_observation"]
    assert MINT not in dex._fail_count


@pytest.mark.asyncio
async def test_newer_failed_generation_not_replaced_by_older_success(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def fetch(url, client, **kwargs):
        if client.index == 0:
            started.set()
            await release.wait()
            return payload(1)
        return None
    monkeypatch.setattr(dex, "_fetch_json", fetch)
    older = asyncio.create_task(dex.get_pair(MINT))
    await asyncio.wait_for(started.wait(), 1)
    fresh = await dex.get_pair(MINT, force_refresh=True)
    release.set()
    assert (await older)["price_usd"] == 1 and fresh is None
    assert await dex.get_pair(MINT) is None
    assert dex._fail_count[MINT] == 1


@pytest.mark.asyncio
async def test_normal_follower_joins_latest_force_not_superseded_owner(monkeypatch, isolated):
    old_gate, new_gate, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def fetch(url, client, **kwargs):
        started.set()
        await (old_gate if client.index == 0 else new_gate).wait()
        return payload(client.index + 1)
    monkeypatch.setattr(dex, "_fetch_json", fetch)
    old = asyncio.create_task(dex.get_pair(MINT))
    await asyncio.wait_for(started.wait(), 1)
    new = asyncio.create_task(dex.get_pair(MINT, force_refresh=True))
    await settle()
    follower = asyncio.create_task(dex.get_pair(MINT))
    await settle()
    owners = len(isolated)
    new_gate.set()
    new_row, follower_row = await asyncio.gather(new, follower)
    old_gate.set()
    await old
    assert owners == 2 and new_row["price_usd"] == follower_row["price_usd"] == 2
    assert new_row is not follower_row


@pytest.mark.asyncio
async def test_force_capacity_rejection_preserves_good_cache_and_backoff(monkeypatch):
    monkeypatch.setattr(dex, "_REQUESTS", OwnedRequests(1, 1))
    started, release = asyncio.Event(), asyncio.Event()
    async def fetch(url, client, **kwargs):
        started.set()
        await release.wait()
        return payload(1, OTHER)
    monkeypatch.setattr(dex, "_fetch_json", fetch)
    held = asyncio.create_task(dex.get_pair(OTHER))
    await asyncio.wait_for(started.wait(), 1)
    original = dex._stamp_pair_observation(payload(3)[0])
    cache.cache_set("dex:" + MINT, deepcopy(original), ttl=60)
    assert await dex.get_pair(MINT, force_refresh=True) is None
    after = await dex.get_pair(MINT)
    release.set()
    await held
    assert after == original and MINT not in dex._fail_count
    assert dex.request_runtime_snapshot()["requests"]["capacity_rejections"] == 1


@pytest.mark.asyncio
async def test_joiner_capacity_and_cancellation_do_not_cancel_owner(monkeypatch):
    monkeypatch.setattr(dex, "_REQUESTS", OwnedRequests(1, 1))
    started, release = asyncio.Event(), asyncio.Event()
    async def fetch(*args, **kwargs):
        started.set()
        await release.wait()
        return payload()
    monkeypatch.setattr(dex, "_fetch_json", fetch)
    owner = asyncio.create_task(dex.get_pair(MINT))
    await asyncio.wait_for(started.wait(), 1)
    joiner = asyncio.create_task(dex.get_pair(MINT))
    await settle()
    assert await dex.get_pair(MINT) is None
    joiner.cancel()
    with pytest.raises(asyncio.CancelledError): await joiner
    assert not owner.done() and dex._REQUESTS.snapshot()["owners"] == 1
    release.set()
    assert (await owner)["price_usd"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [False, True])
async def test_cancel_and_timeout_drain_native_session_under_repeated_stop(monkeypatch, timeout):
    held, started, closing, finish = [asyncio.Event() for _ in range(4)]
    clients = []
    class Client:
        closed = 0
        async def __aenter__(self): clients.append(self); return self
        async def __aexit__(self, *args):
            closing.set()
            await finish.wait()
            self.closed += 1
    monkeypatch.setattr(dex.aiohttp, "ClientSession", Client)
    async def fetch(*args, **kwargs):
        started.set()
        await held.wait()
        return payload()
    monkeypatch.setattr(dex, "_fetch_json", fetch)
    work = dex.get_pair(MINT)
    owner = asyncio.create_task(asyncio.wait_for(work, .05) if timeout else work)
    await asyncio.wait_for(started.wait(), 1)
    joiner = asyncio.create_task(dex.get_pair(MINT))
    await settle()
    if not timeout: owner.cancel()
    await asyncio.wait_for(closing.wait(), 1)
    if not timeout: owner.cancel(); owner.cancel()
    await settle()
    assert not owner.done() and dex._REQUESTS.snapshot()["owners"] == 1
    assert dex.request_runtime_snapshot()["latest_generations"] == 1
    finish.set()
    with pytest.raises(asyncio.TimeoutError if timeout else asyncio.CancelledError): await owner
    assert await joiner is None and clients[0].closed == 1
    assert cache.cache_get("dex:" + MINT) is None and MINT not in dex._fail_count


@pytest.mark.asyncio
async def test_different_mints_make_independent_progress(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def fetch(url, client, **kwargs):
        if MINT in url:
            started.set()
            await release.wait()
            return payload(1)
        return payload(2, OTHER)
    monkeypatch.setattr(dex, "_fetch_json", fetch)
    held = asyncio.create_task(dex.get_pair(MINT))
    await asyncio.wait_for(started.wait(), 1)
    fast = await asyncio.wait_for(dex.get_pair(OTHER), 1)
    release.set()
    await held
    assert fast["address"] == OTHER and fast["price_usd"] == 2


@pytest.mark.parametrize("old_failed", [False, True])
@pytest.mark.parametrize("forced", [False, True])
def test_latest_admission_owns_cache_across_loops_threads(monkeypatch, old_failed, forced):
    started, release = threading.Event(), threading.Event()
    async def fetch(url, client, **kwargs):
        if client.index == 0:
            started.set()
            while not release.is_set(): await asyncio.sleep(.001)
            return None if old_failed else payload(1)
        return payload(2)
    monkeypatch.setattr(dex, "_fetch_json", fetch)
    def read(force=False): return asyncio.run(dex.get_pair(MINT, force_refresh=force))
    with ThreadPoolExecutor(max_workers=2) as workers:
        older = workers.submit(read)
        try:
            assert started.wait(2)
            newer = workers.submit(read, forced).result(timeout=2)
        finally:
            release.set()
        old = older.result(timeout=2)
    assert newer["price_usd"] == 2
    assert (old is None) if old_failed else old["price_usd"] == 1
    cached = asyncio.run(dex.get_pair(MINT))
    assert cached["price_usd"] == 2 and cached["market_observation"] == newer["market_observation"]
    assert MINT not in dex._fail_count


@pytest.mark.asyncio
async def test_admission_order_precedes_deferred_child_start(monkeypatch):
    requests = dex._REQUESTS
    original = requests._await_owned
    admitted, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def defer_old(factory, name):
        calls.append(name)
        if len(calls) == 1:
            admitted.set()
            await release.wait()
        return await original(factory, name)
    monkeypatch.setattr(requests, "_await_owned", defer_old)
    async def fetch(url, client, **kwargs):
        return payload(2 if client.index == 0 else 1)
    monkeypatch.setattr(dex, "_fetch_json", fetch)
    older = asyncio.create_task(dex.get_pair(MINT))
    await asyncio.wait_for(admitted.wait(), 1)
    fresh = await dex.get_pair(MINT, force_refresh=True)
    release.set()
    assert (await older)["price_usd"] == 1
    cached = await dex.get_pair(MINT)
    assert cached["price_usd"] == 2 and cached["market_observation"] == fresh["market_observation"]


@pytest.mark.asyncio
async def test_shared_native_request_exception_releases_state_and_allows_retry(monkeypatch, isolated):
    started, release = asyncio.Event(), asyncio.Event()
    error = RuntimeError("synthetic provider boundary failure")
    async def fetch(*args, **kwargs):
        started.set()
        await release.wait()
        raise error
    monkeypatch.setattr(dex, "_fetch_json", fetch)
    owner = asyncio.create_task(dex.get_pair(MINT))
    await asyncio.wait_for(started.wait(), 1)
    joiner = asyncio.create_task(dex.get_pair(MINT))
    await settle()
    release.set()
    failures = await asyncio.gather(owner, joiner, return_exceptions=True)
    assert failures == [error, error]
    assert not dex._latest_request and isolated[0].closed == 1
    assert cache.cache_get("dex:" + MINT) is None and MINT not in dex._fail_count
    async def success(*args, **kwargs): return payload(3)
    monkeypatch.setattr(dex, "_fetch_json", success)
    assert (await dex.get_pair(MINT, force_refresh=True))["price_usd"] == 3


@pytest.mark.asyncio
async def test_owned_admission_hook_only_runs_for_accepted_owners():
    requests, gate = OwnedRequests(1, 1), asyncio.Event()
    admitted, calls = [], []
    async def load(): calls.append(len(admitted)); await gate.wait(); return 1
    owner = asyncio.create_task(requests.run("one", load, on_admit=lambda: admitted.append("one")))
    await settle()
    joiner = asyncio.create_task(requests.run("one", load, on_admit=lambda: admitted.append("joiner")))
    await settle()
    assert await requests.run("two", load, on_admit=lambda: admitted.append("rejected")) is None
    assert await requests.run("one", load, force_refresh=True, on_admit=lambda: admitted.append("force")) is None
    gate.set()
    assert await asyncio.gather(owner, joiner) == [1, 1]
    assert admitted == ["one"] and calls == [1]


@pytest.mark.asyncio
async def test_failed_admission_hook_does_not_leak_ownership_or_loop_state():
    requests = OwnedRequests(1, 1)
    def fail(): raise ValueError("synthetic admission failure")
    async def never(): raise AssertionError("factory must not start")
    with pytest.raises(ValueError, match="admission failure"):
        await requests.run("one", never, on_admit=fail)
    state = requests.snapshot()
    assert state["owners"] == state["joiners"] == state["loop_buckets"] == 0


@pytest.mark.parametrize("value", [None, "", "invalid", {}, True])
@pytest.mark.asyncio
async def test_invalid_identity_never_admitted(value, isolated):
    assert await dex.get_pair(value, force_refresh=True) is None
    assert not isolated and dex.request_runtime_snapshot()["requests"]["owners"] == 0


@pytest.mark.asyncio
async def test_child_cancelled_before_factory_starts_releases_admitted_generation(monkeypatch, isolated):
    original = asyncio.create_task
    def cancel_child(coro, **kwargs):
        task = original(coro, **kwargs)
        if kwargs.get("name") == dex._REQUESTS.name:
            task.cancel()
        return task
    monkeypatch.setattr(asyncio, "create_task", cancel_child)
    with pytest.raises(asyncio.CancelledError):
        await dex.get_pair(MINT, force_refresh=True)
    assert not isolated and not dex._latest_request
    assert cache.cache_get("dex:" + MINT) is None and MINT not in dex._fail_count


def test_snapshot_contains_aggregate_pressure_not_addresses_or_credentials():
    snapshot = dex.request_runtime_snapshot()
    encoded = json.dumps(snapshot)
    assert MINT not in encoded and OTHER not in encoded
    assert snapshot["latest_generations"] == 0 and snapshot["requests"]["owners"] == 0
    assert snapshot["requests"]["basis"] == "local_ownership_not_wire_billing_or_entitlement"
