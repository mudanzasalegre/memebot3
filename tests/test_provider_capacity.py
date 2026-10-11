"""Native synthetic cache/request pressure; no bot, provider or profit claim."""
import asyncio
from copy import deepcopy
import importlib
import time

import pytest

from fetcher import birdeye as be, geckoterminal as gt, dexscreener as dex
from utils import simple_cache as cache

MINT = "So11111111111111111111111111111111111111112"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    cache._CACHE.clear()
    be._fail_count.clear()
    gt._fail_count.clear()
    dex._fail_count.clear()
    monkeypatch.setattr(be, "_API_KEY", "synthetic-not-a-credential")
    async def throttle(): pass
    monkeypatch.setattr(be, "_throttle", throttle)
    def forbidden(*a, **k): raise AssertionError("Real HTTP forbidden")
    monkeypatch.setattr(be.aiohttp, "ClientSession", forbidden)
    yield
    for obj in (getattr(be, "_REQUESTS", None), getattr(cache, "_LOADERS", None)):
        if obj is not None:
            assert obj.snapshot()["owners"] == obj.snapshot()["joiners"] == 0
    cache._CACHE.clear()
    be._fail_count.clear()
    gt._fail_count.clear()
    dex._fail_count.clear()


class Response:
    status = 200
    def __init__(self, payload, gate=None): self.payload, self.gate = payload, gate
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def json(self):
        if self.gate is not None: await self.gate.wait()
        return deepcopy(self.payload)


def install_client(monkeypatch, responses):
    calls = []
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, url, **options):
            calls.append((url, options))
            return responses[min(len(calls)-1, len(responses)-1)]
    monkeypatch.setattr(be.aiohttp, "ClientSession", lambda *a, **k: Session())
    return calls


def payload(price=2):
    return {"success": True, "data": {"address": MINT, "price": price,
        "liquidity": 10000, "priceChange5mPercent": 25000}}


async def settle():
    for _ in range(20): await asyncio.sleep(0)


def test_original_shared_cache_capacity_and_lru(monkeypatch):
    monkeypatch.setattr(cache, "_MAX_ENTRIES", 3, raising=False)
    for key in ("a", "b", "c"): cache.cache_set(key, key, ttl=60)
    assert cache.cache_get("a") == "a"
    cache.cache_set("d", "d", ttl=60)
    assert len(cache._CACHE) <= 3 and cache.cache_get("b") is None
    assert cache.cache_get("a") == "a"


def test_original_expired_unread_rows_are_pruned(monkeypatch):
    clock = [1000.]
    monkeypatch.setattr(cache.time, "time", lambda: clock[0])
    for i in range(20): cache.cache_set(f"old-{i}", i, ttl=1)
    clock[0] = 1002.
    cache.cache_set("new", 1, ttl=60)
    assert list(cache._CACHE) == ["new"]


@pytest.mark.asyncio
async def test_original_unrelated_cache_loaders_do_not_block():
    gate, started = asyncio.Event(), asyncio.Event()
    async def slow(): await gate.wait(); return 1
    async def fast(): started.set(); return 2
    first = asyncio.create_task(cache.cache_get_or_set("slow", slow))
    await settle()
    second = asyncio.create_task(cache.cache_get_or_set("fast", fast))
    await settle()
    independent = started.is_set()
    gate.set()
    assert await asyncio.gather(first, second) == [1, 2]
    assert independent


@pytest.mark.asyncio
async def test_original_birdeye_identical_requests_share_one_http(monkeypatch):
    gate = asyncio.Event()
    calls = install_client(monkeypatch, [Response(payload(), gate)])
    tasks = [asyncio.create_task(be.get_token_info(MINT)) for _ in range(8)]
    await settle()
    gate.set()
    results = await asyncio.gather(*tasks)
    assert len(calls) == 1
    assert all(x["price_pct_5m"] == 25000 for x in results)
    assert len({x["market_observation"]["fields"]["price_usd"]["received_at"] for x in results}) == 1
    results[0]["liquidity"]["usd"] = 0
    assert results[1]["liquidity"]["usd"] == 10000


@pytest.mark.asyncio
async def test_original_cancelled_joiner_does_not_duplicate_or_cancel_owner(monkeypatch):
    gate = asyncio.Event()
    calls = install_client(monkeypatch, [Response(payload(), gate)])
    owner = asyncio.create_task(be.get_token_info(MINT))
    await settle()
    joiner = asyncio.create_task(be.get_token_info(MINT))
    await settle()
    joiner.cancel()
    with pytest.raises(asyncio.CancelledError): await joiner
    gate.set()
    assert (await owner)["price_usd"] == 2 and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [{"success":False,"data":{}}, payload(1)])
async def test_original_forced_refresh_cannot_be_overwritten_by_older_request(monkeypatch, late):
    gate = asyncio.Event()
    install_client(monkeypatch, [Response(late, gate), Response(payload(3))])
    older = asyncio.create_task(be.get_token_info(MINT))
    await settle()
    fresh = await be.get_token_info(MINT, force_refresh=True)
    gate.set()
    await older
    assert fresh["price_usd"] == 3
    cached = await be.get_token_info(MINT)
    assert cached is not None and cached["price_usd"] == 3
    assert not be._fail_count


@pytest.mark.asyncio
async def test_original_force_refresh_starts_a_new_request(monkeypatch):
    gate = asyncio.Event()
    calls = install_client(monkeypatch, [Response(payload(1), gate), Response(payload(3))])
    older = asyncio.create_task(be.get_token_info(MINT))
    await settle()
    fresh = await be.get_token_info(MINT, force_refresh=True)
    gate.set()
    await older
    assert len(calls) == 2 and fresh["price_usd"] == 3


@pytest.mark.asyncio
async def test_original_cache_preserves_receipt_and_extreme_signal(monkeypatch):
    calls = install_client(monkeypatch, [Response(payload())])
    first = await be.get_token_info(MINT)
    second = await be.get_token_info(MINT)
    assert first is not second and first["market_observation"] == second["market_observation"]
    assert second["price_pct_5m"] == 25000 and len(calls) == 1


@pytest.mark.parametrize("module", [be, gt])
def test_original_per_key_failure_state_is_capacity_bounded(module):
    for i in range(8200): module._register_fail(f"synthetic-capacity-{i}")
    assert len(module._fail_count) <= 8192


@pytest.mark.parametrize("ttl", [None, True, False, 0, -1, "bad", [], float("nan"), float("inf"), -float("inf"), 10**400])
def test_invalid_ttl_invalidates_without_fabricating_or_raising(ttl):
    from utils.bounded_state import TTLStore
    store = TTLStore(3, clock=lambda: 1000.)
    store.set("key", "original", 10)
    store.set("key", "replacement", ttl)
    assert store.get("key") is None and store.snapshot()["entries"] == 0


@pytest.mark.parametrize("value,expected", [("bad",8192),(True,8192),(0,1),(-1,1),(100000,65536),
    (float("inf"),8192),(float("nan"),8192),("1.5",8192)])
def test_capacity_configuration_is_bounded(value, expected):
    from utils.bounded_state import TTLStore
    assert TTLStore(value).max_entries == expected


@pytest.mark.parametrize("later", [999., float("nan"), float("inf")])
def test_local_clock_reversal_or_invalidity_drops_cache(later):
    from utils.bounded_state import TTLStore
    now = [1000.]
    store = TTLStore(3, clock=lambda: now[0])
    store.set("key", {"received_at": 1000.}, 60)
    now[0] = later
    assert store.get("key") is None and store.snapshot()["entries"] == 0


def test_expiry_index_compacts_under_same_key_rewrites():
    from utils.bounded_state import TTLStore
    now = [1000.]
    store = TTLStore(8, clock=lambda: now[0])
    for i in range(10000): store.set("key", i, 1+i)
    assert store.get("key") == 9999 and store.snapshot()["expiry_records"] <= 64
    now[0] += 2
    assert store.get("key") == 9999
    store.clear()
    assert store.snapshot()["expiry_records"] == 0


def test_expiration_boundary_and_sentinel_identity():
    from utils.bounded_state import TTLStore
    now = [0.]
    store, sentinel = TTLStore(2, clock=lambda: now[0]), object()
    store.set("nil", sentinel, 2)
    assert store.get("nil") is sentinel
    now[0] = 2.
    assert store.get("nil") is None


def test_failure_counts_are_recent_capped_atomic_and_expiring():
    from concurrent.futures import ThreadPoolExecutor
    from utils.bounded_state import BoundedFailureCounter
    now = [0.]
    counts = BoundedFailureCounter(3, 5, clock=lambda: now[0])
    with ThreadPoolExecutor(max_workers=4) as workers:
        assert all(x <= 4 for x in workers.map(lambda _: counts.increment("a"), range(100)))
    assert counts["a"] == 4
    for key in ("b", "c", "d"): counts.increment(key)
    assert len(counts) == 3 and "a" not in counts
    now[0] = 5.
    assert not counts and counts.increment("a") == 1
    counts.pop("a")
    assert not counts


def test_shared_cache_is_thread_safe_and_capacity_bounded(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(cache, "_MAX_ENTRIES", 17)
    def operation(i):
        cache.cache_set(str(i), i, 60)
        cache.cache_get(str(i))
        if i % 3 == 0: cache.cache_delete(str(i))
    with ThreadPoolExecutor(max_workers=8) as workers: list(workers.map(operation, range(2000)))
    snapshot = cache.cache_snapshot()
    assert snapshot["entries"] <= 17 and snapshot["expiry_records"] <= 64


@pytest.mark.parametrize("maximum,joiners", [("bad","bad"),(float("inf"),float("nan")),(0,0),(999999,999999)])
def test_request_limits_parse_safely(maximum, joiners):
    from utils.request_ownership import OwnedRequests
    requests = OwnedRequests(maximum, joiners)
    assert 1 <= requests.max_owners <= 128 and 1 <= requests.max_joiners <= 4096


@pytest.mark.asyncio
async def test_request_capacity_is_nonblocking_and_does_not_load_rejected_work():
    from utils.request_ownership import OwnedRequests
    requests, gate, calls = OwnedRequests(1, 1), asyncio.Event(), []
    async def load(): calls.append(1); await gate.wait(); return 5
    owner = asyncio.create_task(requests.run("a", load))
    await settle()
    joiner = asyncio.create_task(requests.run("a", load))
    await settle()
    assert await requests.run("b", load) is None
    assert await requests.run("a", load) is None
    assert await requests.run("a", load, force_refresh=True) is None
    assert requests.snapshot()["owners"] == requests.snapshot()["joiners"] == 1
    gate.set()
    assert await asyncio.gather(owner, joiner) == [5, 5] and calls == [1]
    assert requests.snapshot()["loop_buckets"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [False, True])
async def test_repeated_owner_cancel_or_timeout_drains_cleanup(timeout):
    from utils.request_ownership import OwnedRequests
    requests = OwnedRequests(1, 2)
    cleaning, finish, held = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def load():
        try: await held.wait()
        finally:
            cleaning.set()
            await finish.wait()
    work = requests.run("a", load)
    owner = asyncio.create_task(asyncio.wait_for(work, .05) if timeout else work)
    await settle()
    joiner = asyncio.create_task(requests.run("a", load))
    await settle()
    if not timeout: owner.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    if not timeout: owner.cancel(); owner.cancel()
    await settle()
    assert not owner.done() and requests.snapshot()["owners"] == 1
    finish.set()
    with pytest.raises(asyncio.TimeoutError if timeout else asyncio.CancelledError): await owner
    assert await joiner is None
    assert requests.snapshot()["owners"] == requests.snapshot()["joiners"] == requests.snapshot()["loop_buckets"] == 0


@pytest.mark.asyncio
async def test_joiner_timeout_does_not_release_owner():
    from utils.request_ownership import OwnedRequests
    requests, gate = OwnedRequests(1, 1), asyncio.Event()
    async def load(): await gate.wait(); return "original"
    owner = asyncio.create_task(requests.run("a", load))
    await settle()
    with pytest.raises(asyncio.TimeoutError): await asyncio.wait_for(requests.run("a", load), .01)
    assert requests.snapshot()["owners"] == 1 and requests.snapshot()["joiners"] == 0
    gate.set()
    assert await owner == "original"


@pytest.mark.asyncio
async def test_shared_exception_releases_all_state():
    from utils.request_ownership import OwnedRequests
    requests, gate = OwnedRequests(), asyncio.Event()
    async def load(): await gate.wait(); raise ValueError("synthetic")
    owner = asyncio.create_task(requests.run("a", load))
    await settle()
    joiner = asyncio.create_task(requests.run("a", load))
    await settle(); gate.set()
    results = await asyncio.gather(owner, joiner, return_exceptions=True)
    assert all(isinstance(x, ValueError) for x in results)
    assert requests.snapshot()["owners"] == requests.snapshot()["joiners"] == requests.snapshot()["loop_buckets"] == 0


def test_request_state_does_not_bind_to_finished_loops():
    from utils.request_ownership import OwnedRequests
    requests = OwnedRequests()
    async def load(): return 5
    for _ in range(5):
        assert asyncio.run(requests.run("key", load)) == 5
        assert requests.snapshot()["loop_buckets"] == 0


@pytest.mark.asyncio
async def test_native_birdeye_capacity_does_not_poison_cache_or_backoff(monkeypatch):
    from utils.request_ownership import OwnedRequests
    monkeypatch.setattr(be, "_REQUESTS", OwnedRequests(1, 1))
    gate = asyncio.Event()
    calls = install_client(monkeypatch, [Response(payload(), gate)])
    owner = asyncio.create_task(be.get_token_info(MINT))
    await settle()
    assert await be.get_token_info(MINT, force_refresh=True) is None
    assert not be._fail_count and cache.cache_get(f"be:v2:token:{MINT}") is None
    gate.set()
    assert (await owner)["price_usd"] == 2 and len(calls) == 1


@pytest.mark.asyncio
async def test_normal_call_joins_the_latest_forced_request(monkeypatch):
    gate = asyncio.Event()
    calls = install_client(monkeypatch, [Response(payload(3), gate)])
    forced = asyncio.create_task(be.get_token_info(MINT, force_refresh=True))
    await settle()
    ordinary = asyncio.create_task(be.get_token_info(MINT))
    await settle(); gate.set()
    assert all(x["price_usd"] == 3 for x in await asyncio.gather(forced, ordinary))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_birth_timeout_drains_native_birdeye_cleanup(monkeypatch):
    from runtime.token_birth_enrichment import TokenBirthResolver
    cleaning, finish, blocked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class ClosingResponse(Response):
        async def __aexit__(self, *a):
            cleaning.set()
            await finish.wait()
    install_client(monkeypatch, [ClosingResponse({}, blocked)])
    resolver = TokenBirthResolver(configured=lambda: True, degraded=lambda: False, timeout_s=.05)
    owner = asyncio.create_task(resolver.enrich({"address": MINT}))
    await asyncio.wait_for(cleaning.wait(), 1)
    assert not owner.done() and resolver.snapshot()["inflight"] == 1 and be._REQUESTS.snapshot()["owners"] == 1
    finish.set()
    result = await owner
    assert result["token_birth_enrichment"]["status"] == "timeout" and "created_at" not in result
    assert not be._fail_count and be.request_runtime_snapshot()["latest_generations"] == 0


@pytest.mark.parametrize("name", ["fetcher.birdeye", "fetcher.dexscreener", "fetcher.geckoterminal"])
def test_native_cold_provider_import_with_malformed_optional_settings(monkeypatch, name):
    from pathlib import Path
    for key in ("MEMORY_CACHE_MAX_ENTRIES", "PROVIDER_FAILURE_MAX_ENTRIES", "PROVIDER_FAILURE_RETENTION_S",
        "BIRDEYE_MAX_INFLIGHT", "BIRDEYE_MAX_JOINERS", "DEXS_TTL_NIL_SHORT", "DEXS_TTL_NIL_MAX",
        "GECKO_TTL_NIL_SHORT", "GECKO_TTL_NIL_MAX"):
        monkeypatch.setenv(key, "malformed")
    monkeypatch.setenv("BIRDEYE_API_KEY", "synthetic-not-a-credential")
    original = importlib.import_module(name)
    spec = importlib.util.spec_from_file_location("isolated_capacity_import", original.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve().is_relative_to(Path.cwd().resolve())
    assert module._fail_count.snapshot()["capacity"] == 8192
    assert module._fail_count.retention_s == 3600 and module._TTL_NIL_MAX >= module._TTL_NIL_SHORT > 0


def test_capacity_snapshot_contains_no_mints_or_values():
    import json
    cache.cache_set("private-cache-key", {"address": MINT}, 60)
    raw = json.dumps({"cache": cache.cache_snapshot(), "provider": be.request_runtime_snapshot()})
    assert MINT not in raw and "private-cache-key" not in raw and "synthetic-not-a-credential" not in raw


def test_cross_loop_owner_capacity_is_global_and_idle_loops_are_removed():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from utils.request_ownership import OwnedRequests
    requests = OwnedRequests(2, 2)
    ready, release = threading.Barrier(3), threading.Event()
    async def load():
        ready.wait(timeout=2)
        while not release.is_set(): await asyncio.sleep(.001)
        return 5
    def run(): return asyncio.run(requests.run("same-key", load))
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(run) for _ in range(2)]
        ready.wait(timeout=2)
        observed = requests.snapshot()
        async def unused(): raise AssertionError("capacity-rejected factory ran")
        rejected = asyncio.run(requests.run("third-key", unused))
        release.set()
        results = [f.result(timeout=2) for f in futures]
    assert observed["owners"] == observed["loop_buckets"] == 2 and rejected is None and results == [5, 5]
    assert requests.snapshot()["owners"] == requests.snapshot()["loop_buckets"] == 0


@pytest.mark.parametrize("late", [{"success":False,"data":{}}, payload(1)])
def test_native_cross_loop_late_response_cannot_overwrite_force_refresh(monkeypatch, late):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    ready, release = threading.Event(), threading.Event()
    class ThreadResponse(Response):
        async def json(self):
            ready.set()
            while not release.is_set(): await asyncio.sleep(.001)
            return deepcopy(self.payload)
    install_client(monkeypatch, [ThreadResponse(late), Response(payload(3))])
    with ThreadPoolExecutor(max_workers=1) as worker:
        older = worker.submit(lambda: asyncio.run(be.get_token_info(MINT)))
        assert ready.wait(timeout=2)
        try: fresh = asyncio.run(be.get_token_info(MINT, force_refresh=True))
        finally: release.set()
        older.result(timeout=2)
    assert fresh["price_usd"] == asyncio.run(be.get_token_info(MINT))["price_usd"] == 3
    assert not be._fail_count and be.request_runtime_snapshot()["latest_generations"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {"received_at":123.,"signal":25000}])
async def test_generic_cache_loader_coalesces_without_relabeling_result(result):
    gate, calls = asyncio.Event(), []
    async def load(): calls.append(1); await gate.wait(); return result
    tasks = [asyncio.create_task(cache.cache_get_or_set("key", load)) for _ in range(8)]
    await settle(); gate.set()
    values = await asyncio.gather(*tasks)
    assert calls == [1] and all(value is result for value in values)


def test_runtime_capacity_snapshot_uses_native_aggregate_functions():
    import ast
    from pathlib import Path
    source = ast.parse((Path.cwd()/"run_bot.py").read_text(encoding="utf-8"))
    function = next(x for x in source.body if isinstance(x, ast.AsyncFunctionDef) and x.name=="_build_runtime_state_snapshot")
    assignment = next(x for x in ast.walk(function) if isinstance(x, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id=="stats_payload" for t in x.targets))
    index = next(i for i,key in enumerate(assignment.value.keys) if isinstance(key,ast.Constant) and key.value=="provider_capacity")
    expression = ast.Expression(assignment.value.values[index])
    result = eval(compile(ast.fix_missing_locations(expression), "native-runtime-snapshot", "eval"),
        {"cache_snapshot":cache.cache_snapshot,"birdeye_pressure_snapshot":be.request_runtime_snapshot})
    assert result["shared_cache"]["capacity"] == 8192 and result["birdeye"]["requests"]["owners"] == 0


@pytest.mark.asyncio
async def test_native_dex_failure_backoff_escalation_and_success_reset(monkeypatch):
    install_client(monkeypatch, [Response({})])
    async def unavailable(*a, **k): return None
    monkeypatch.setattr(dex, "_fetch_json", unavailable)
    for _ in range(4): assert await dex.get_pair(MINT, force_refresh=True) is None
    assert dex._fail_count[MINT] == 4
    assert cache._CACHE[f"dex:{MINT}"][0] - time.time() >= dex._TTL_NIL_MAX - 2
    async def available(*a, **k):
        return [{"chainId":"solana","baseToken":{"address":MINT},"pairAddress":"1"*32,
                 "priceUsd":"3","liquidity":{"usd":10000},"volume":{"h24":1000}}]
    monkeypatch.setattr(dex, "_fetch_json", available)
    assert (await dex.get_pair(MINT, force_refresh=True))["price_usd"] == 3
    assert MINT not in dex._fail_count
