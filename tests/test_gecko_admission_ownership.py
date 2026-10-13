"""Native Gecko admitted-generation/capacity regressions; no real HTTP."""
import asyncio
from copy import deepcopy
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from fetcher import geckoterminal as gt
from utils import simple_cache
from utils.bounded_state import BoundedFailureCounter
from utils.request_ownership import OwnedRequests

MINT = "So11111111111111111111111111111111111111112"
OTHER = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def payload(address=MINT, price=2):
    return {"data": {"id": f"solana_{address}", "type": "token",
        "attributes": {"address": address, "price_usd": str(price),
            "market_cap_usd": None, "fdv_usd": "1000",
            "total_reserve_in_usd": "5000", "volume_usd": {"h24": "100"}}}}


class Limiter:
    max_calls = 10
    def acquire_sync(self): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


class AsyncResponse:
    status = 200
    headers = {}
    def __init__(self, address=MINT, price=2, block=False):
        self.body = payload(address, price)
        self.block = block
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.exited = 0
    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.exited += 1
    async def json(self):
        self.started.set()
        if self.block: await self.release.wait()
        return deepcopy(self.body)


class AsyncClient:
    def __init__(self, response): self.response, self.calls = response, []
    def get(self, url, **kwargs):
        assert url.startswith("https://api.geckoterminal.com/api/v2/networks/solana/tokens/")
        assert kwargs["allow_redirects"] is False
        self.calls.append((url, kwargs))
        return self.response


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    assert Path(gt.__file__).resolve().is_relative_to(Path.cwd().resolve())
    simple_cache.cache_clear()
    monkeypatch.setattr(gt, "USE_GECKO_TERMINAL", True)
    monkeypatch.setattr(gt, "_REQUESTS", OwnedRequests(name="gecko-native-diagnostic"))
    monkeypatch.setattr(gt, "_state_lock", threading.RLock())
    monkeypatch.setattr(gt, "_latest_request", {})
    monkeypatch.setattr(gt, "_active_requests", 0)
    monkeypatch.setattr(gt, "_fail_count", BoundedFailureCounter())
    monkeypatch.setattr(gt, "_min_interval_s", 0.)
    monkeypatch.setattr(gt, "_last_call_ts", 0.)
    monkeypatch.setattr(gt, "_last_observed_ts", None)
    monkeypatch.setattr(gt, "_cooldown_until", 0.)
    monkeypatch.setattr(gt, "GECKO_LIMITER", Limiter())
    def forbidden(*args, **kwargs):
        raise AssertionError("Real HTTP forbidden in native diagnosis")
    monkeypatch.setattr(gt.requests, "get", forbidden)
    monkeypatch.setattr(gt.requests, "Session", forbidden)
    if gt.aiohttp is not None:
        monkeypatch.setattr(gt.aiohttp, "ClientSession", forbidden)
    yield
    state = gt.request_runtime_snapshot()
    assert state["active_requests"] == state["latest_generations"] == 0
    assert state["requests"]["owners"] == state["requests"]["joiners"] == 0
    simple_cache.cache_clear()


@pytest.mark.asyncio
async def test_older_admitted_owner_cannot_publish_after_newer_force(monkeypatch):
    held, release = asyncio.Event(), asyncio.Event()
    original_await = gt._REQUESTS._await_owned
    first = True
    async def delay_first_factory(factory, name):
        nonlocal first
        if first:
            first = False
            held.set()
            await release.wait()
        return await original_await(factory, name)
    monkeypatch.setattr(gt._REQUESTS, "_await_owned", delay_first_factory)
    old_client = AsyncClient(AsyncResponse(price=1))
    fresh_client = AsyncClient(AsyncResponse(price=2))
    old = asyncio.create_task(gt.get_token_data_async("solana", MINT, old_client))
    try:
        await asyncio.wait_for(held.wait(), 2)
        fresh = await gt.get_token_data_async("solana", MINT, fresh_client, force_refresh=True)
        assert fresh["price_usd"] == 2
    finally:
        release.set()
        older = await asyncio.wait_for(old, 2)
    assert older["price_usd"] == 1
    cached = await gt.get_token_data_async("solana", MINT, fresh_client)
    assert cached["price_usd"] == 2
    assert cached["market_observation"] == fresh["market_observation"]
    assert len(old_client.calls) == len(fresh_client.calls) == 1


@pytest.mark.asyncio
async def test_capacity_rejected_force_cannot_invalidate_existing_receipt(monkeypatch):
    monkeypatch.setattr(gt._REQUESTS, "max_owners", 1)
    client = AsyncClient(AsyncResponse())
    original = await gt.get_token_data_async("solana", MINT, client, force_refresh=True)
    occupied = AsyncClient(AsyncResponse(address=OTHER, block=True))
    owner = asyncio.create_task(gt.get_token_data_async("solana", OTHER, occupied))
    try:
        await asyncio.wait_for(occupied.response.started.wait(), 2)
        rejected = await gt.get_token_data_async("solana", MINT, client, force_refresh=True)
        assert rejected is None and len(client.calls) == 1
        found, hit = gt._cached(f"gt:{gt._CONTRACT}:solana:{MINT}", MINT)
    finally:
        occupied.response.release.set()
        await asyncio.wait_for(owner, 2)
    assert found is True
    assert hit["market_observation"] == original["market_observation"]


@pytest.mark.asyncio
async def test_shared_adapter_capacity_rejection_preserves_cached_receipt():
    client = AsyncClient(AsyncResponse())
    original = await gt.get_token_data_async("solana", MINT, client, force_refresh=True)
    leases = [(f"occupied-{i}", gt._begin_request(f"occupied-{i}")) for i in range(gt._MAX_ACTIVE)]
    try:
        rejected = await gt.get_token_data_async("solana", MINT, client, force_refresh=True)
        assert rejected is None and len(client.calls) == 1
        found, hit = gt._cached(f"gt:{gt._CONTRACT}:solana:{MINT}", MINT)
        assert found and hit["market_observation"] == original["market_observation"]
        assert gt._REQUESTS.snapshot()["owners"] == 0
    finally:
        for key, generation in leases: gt._end_request(key, generation)


@pytest.mark.asyncio
async def test_child_cancelled_before_factory_starts_releases_admitted_capacity(monkeypatch):
    original_create = gt._REQUESTS._await_owned.__globals__["asyncio"].create_task
    def cancel_child(coro, **kwargs):
        task = original_create(coro, **kwargs)
        task.cancel()
        return task
    client = AsyncClient(AsyncResponse())
    monkeypatch.setattr(asyncio, "create_task", cancel_child)
    with pytest.raises(asyncio.CancelledError):
        await gt.get_token_data_async("solana", MINT, client, force_refresh=True)
    assert client.calls == []
    assert gt._fail_count.get(f"gt:{gt._CONTRACT}:solana:{MINT}", 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("old_failed", [False, True])
async def test_older_late_child_cannot_replace_newer_failure(monkeypatch, old_failed):
    held, release = asyncio.Event(), asyncio.Event()
    original_await = gt._REQUESTS._await_owned
    first = True
    async def delay_first(factory, name):
        nonlocal first
        if first:
            first = False
            held.set()
            await release.wait()
        return await original_await(factory, name)
    monkeypatch.setattr(gt._REQUESTS, "_await_owned", delay_first)
    older_client = AsyncClient(AsyncResponse(price=1))
    if old_failed: older_client.response.status = 503
    newer_client = AsyncClient(AsyncResponse(price=2))
    newer_client.response.status = 503
    old = asyncio.create_task(gt.get_token_data_async("solana", MINT, older_client))
    try:
        await asyncio.wait_for(held.wait(), 2)
        assert await gt.get_token_data_async("solana", MINT, newer_client, force_refresh=True) is None
    finally:
        release.set()
        await asyncio.wait_for(old, 2)
    found, hit = gt._cached(f"gt:{gt._CONTRACT}:solana:{MINT}", MINT)
    assert found and hit is None
    assert gt._fail_count.get(f"gt:{gt._CONTRACT}:solana:{MINT}") == 1


@pytest.mark.asyncio
async def test_compatible_joiner_shares_receipt_without_second_adapter_lease():
    client = AsyncClient(AsyncResponse(block=True))
    owner = asyncio.create_task(gt.get_token_data_async("solana", MINT, client))
    await asyncio.wait_for(client.response.started.wait(), 2)
    joiner = asyncio.create_task(gt.get_token_data_async("solana", MINT, client))
    for _ in range(5): await asyncio.sleep(0)
    assert gt._REQUESTS.snapshot()["joiners"] == 1 and gt._active_requests == 1
    client.response.release.set()
    rows = await asyncio.gather(owner, joiner)
    assert len(client.calls) == client.response.exited == 1
    assert rows[0] == rows[1] and rows[0] is not rows[1]
    rows[0]["price_usd"] = 99
    assert rows[1]["price_usd"] == 2


@pytest.mark.asyncio
async def test_cancelled_joiner_does_not_release_or_cancel_owner_lease():
    client = AsyncClient(AsyncResponse(block=True))
    owner = asyncio.create_task(gt.get_token_data_async("solana", MINT, client))
    await asyncio.wait_for(client.response.started.wait(), 2)
    joiner = asyncio.create_task(gt.get_token_data_async("solana", MINT, client))
    for _ in range(5): await asyncio.sleep(0)
    joiner.cancel()
    with pytest.raises(asyncio.CancelledError): await joiner
    assert not owner.done() and gt._active_requests == 1
    client.response.release.set()
    assert (await owner)["price_usd"] == 2


@pytest.mark.parametrize("old_failed", [False, True])
def test_original_admission_order_survives_different_loop_threads(monkeypatch, old_failed):
    held, release = threading.Event(), threading.Event()
    original_await = gt._REQUESTS._await_owned
    older_thread = [None]
    async def hold_older(factory, name):
        if threading.get_ident() == older_thread[0]:
            held.set()
            assert release.wait(2)
        return await original_await(factory, name)
    monkeypatch.setattr(gt._REQUESTS, "_await_owned", hold_older)
    old_client = AsyncClient(AsyncResponse(price=1))
    if old_failed: old_client.response.status = 503
    fresh_client = AsyncClient(AsyncResponse(price=2))
    def older():
        older_thread[0] = threading.get_ident()
        return asyncio.run(gt.get_token_data_async("solana", MINT, old_client))
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(older)
        try:
            assert held.wait(2)
            fresh = pool.submit(lambda: asyncio.run(gt.get_token_data_async(
                "solana", MINT, fresh_client, force_refresh=True))).result(timeout=2)
        finally:
            release.set()
        previous = old.result(timeout=2)
    assert previous is None if old_failed else previous["price_usd"] == 1
    cached = asyncio.run(gt.get_token_data_async("solana", MINT, fresh_client))
    assert cached["price_usd"] == 2 and cached["market_observation"] == fresh["market_observation"]
    assert len(fresh_client.calls) == len(old_client.calls) == 1
    assert gt._fail_count.get(f"gt:{gt._CONTRACT}:solana:{MINT}", 0) == 0


@pytest.mark.asyncio
async def test_cache_invalidation_exception_allocates_no_adapter_or_loop_lease(monkeypatch):
    def fail(key): raise RuntimeError("Synthetic cache invalidation failure")
    monkeypatch.setattr(gt, "cache_delete", fail)
    client = AsyncClient(AsyncResponse())
    with pytest.raises(RuntimeError, match="cache invalidation"):
        await gt.get_token_data_async("solana", MINT, client, force_refresh=True)
    assert client.calls == []
    assert gt._REQUESTS.snapshot()["loop_buckets"] == 0
