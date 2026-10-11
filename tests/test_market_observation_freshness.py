from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
import time
from types import SimpleNamespace
import math

import pytest

from fetcher import birdeye, dexscreener, geckoterminal, jupiter_price
from utils import price_service, simple_cache
from utils.market_observation import (
    fresh_market_value, liquidity_crushed, retain_fresh_market_fields,
    stamp_market_observation,
)

MINT = "A" * 44
OTHER = "B" * 44


@pytest.fixture(autouse=True)
def isolated_sources(monkeypatch):
    from jupiter_access_fixtures import isolate_budget
    isolate_budget(monkeypatch)
    simple_cache._CACHE.clear()
    jupiter_price.clear_caches()
    monkeypatch.setattr(price_service, "_RETRY_ON_FAIL", 0)
    monkeypatch.setattr(price_service, "_USE_BIRDEYE", False)
    monkeypatch.setattr(price_service, "USE_GECKO_TERMINAL", False)
    monkeypatch.setattr(price_service, "_USE_JUPITER_IMPACT", False)
    yield
    simple_cache._CACHE.clear()
    jupiter_price.clear_caches()


def observed(**values):
    return stamp_market_observation({"address": MINT, **values}, "dexscreener")


@pytest.mark.parametrize("field,value", [("price_usd", 2), ("liquidity_usd", 0),
    ("txns_last_5m", 0), ("price_pct_5m", 0), ("price_pct_5m", -80), ("volume_pct_5m", -100)])
def test_zero_and_signed_observations_are_not_missing(field, value):
    tick = observed(**{field: value})
    assert fresh_market_value(tick, field) == value


@pytest.mark.parametrize("value", [None, True, False, float("nan"), float("inf"), -1, 0])
def test_invalid_price_never_becomes_a_fresh_quote(value):
    assert fresh_market_value(observed(price_usd=value), "price_usd") is None


@pytest.mark.parametrize("corruption", ["no_proof", "stale", "future", "value", "mint", "version", "boolean_time"])
def test_receipt_proof_rejects_unknown_stale_or_mismatched_data(corruption):
    tick = observed(price_usd=2)
    record = tick["market_observation"]["fields"]["price_usd"]
    if corruption == "no_proof": tick.pop("market_observation")
    elif corruption == "stale": record["received_at"] = time.time() - 121
    elif corruption == "future": record["received_at"] = time.time() + 60
    elif corruption == "value": tick["price_usd"] = 3
    elif corruption == "mint": tick["address"] = OTHER
    elif corruption == "version": tick["market_observation"]["version"] = "legacy"
    else: record["received_at"] = True
    assert fresh_market_value(tick, "price_usd") is None


def test_new_price_does_not_refresh_old_liquidity_or_resurrect_aliases():
    tick = observed(price_usd=2, liquidity_usd=100, price_pct_5m=0)
    tick["market_observation"]["fields"]["liquidity_usd"]["received_at"] -= 121
    tick["liquidity"] = {"usd": 900}
    tick["priceUsd"] = 123
    filtered = price_service._coerce_tick_numbers(retain_fresh_market_fields(tick))
    assert filtered["price_usd"] == 2 and filtered["liquidity_usd"] is None
    assert filtered["price_pct_5m"] == 0
    assert tick["liquidity"]["usd"] == 900


def test_merge_moves_values_and_original_field_receipts_together():
    primary = stamp_market_observation({"address": MINT, "price_usd": 2}, "jupiter")
    secondary = observed(price_usd=9, liquidity_usd=0, txns_last_5m=0, price_pct_5m=0)
    combined = price_service._merge_market_fields(primary, secondary, "dexscreener")
    assert fresh_market_value(combined, "price_usd", source="jupiter") == 2
    assert fresh_market_value(combined, "liquidity_usd", source="dexscreener") == 0
    later = observed(liquidity_usd=1000, txns_last_5m=100, price_pct_5m=50)
    combined = price_service._merge_market_fields(combined, later, "dexscreener")
    assert combined["liquidity_usd"] == combined["txns_last_5m"] == combined["price_pct_5m"] == 0
    combined["market_observation"]["fields"]["price_usd"]["value"] = 99
    assert primary["market_observation"]["fields"]["price_usd"]["value"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, {"address": MINT, "price_usd": 123}, None])
@pytest.mark.parametrize("critical", [False, True])
async def test_fresh_request_bypasses_positive_negative_and_partial_caches(monkeypatch, cached, critical):
    key = f"price:{MINT}:0:1"
    if cached is not None: simple_cache.cache_set(key, cached, ttl=999)
    simple_cache.cache_set(key + ":partial", {"address": MINT, "price_usd": 456}, ttl=999)
    calls = []
    async def info(address, *, force_refresh=False):
        calls.append((address, force_refresh))
        return jupiter_price.PriceInfo("OK", 2, True, 1, received_at=time.time())
    monkeypatch.setattr(price_service, "_jup_get_price_info", info)
    tick = await price_service.get_price(MINT, price_only=True, allow_partial=True,
        force_refresh=not critical, critical=critical)
    assert tick["price_usd"] == 2 and calls == [(MINT, True)]
    tick["market_observation"]["fields"]["price_usd"]["value"] = 99
    assert simple_cache.cache_get(key)["market_observation"]["fields"]["price_usd"]["value"] == 2


@pytest.mark.asyncio
async def test_forced_fallback_keeps_source_identity_and_zero_liquidity(monkeypatch):
    async def no_info(*args, **kwargs): return jupiter_price.PriceInfo("NIL", None, False, 0)
    calls = []
    async def pair(address, *, force_refresh=False):
        calls.append(force_refresh)
        return observed(price_usd=2, liquidity_usd=0)
    monkeypatch.setattr(price_service, "_jup_get_price_info", no_info)
    monkeypatch.setattr(dexscreener, "get_pair", pair)
    tick = await price_service.get_price(MINT, force_refresh=True)
    assert tick["price_source"] == "dexscreener" and calls == [True]
    assert fresh_market_value(tick, "liquidity_usd") == 0
    assert await price_service.get_jupiter_price_snapshot(MINT) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["wrong_mint", "stale", "no_proof", "native_only"])
async def test_fresh_fallback_does_not_accept_unverified_or_derived_values(monkeypatch, kind):
    async def no_info(*args, **kwargs): return jupiter_price.PriceInfo("NIL", None, False, 0)
    async def pair(*args, **kwargs):
        tick = observed(price_usd=2, liquidity_usd=100)
        if kind == "wrong_mint": tick["address"] = OTHER
        elif kind == "stale":
            for record in tick["market_observation"]["fields"].values(): record["received_at"] -= 121
        elif kind == "no_proof": tick.pop("market_observation")
        else: tick = observed(price_native=.01)
        return tick
    async def forbidden_conversion(): pytest.fail("fresh path must not use cached SOL conversion")
    monkeypatch.setattr(price_service, "_jup_get_price_info", no_info)
    monkeypatch.setattr(dexscreener, "get_pair", pair)
    monkeypatch.setattr(price_service, "get_sol_usd", forbidden_conversion)
    assert await price_service.get_price(MINT, force_refresh=True) is None


@pytest.mark.asyncio
async def test_liquidity_only_probe_does_not_fetch_jupiter_or_require_price(monkeypatch):
    async def forbidden(*args, **kwargs): pytest.fail("liquidity probe requested Jupiter")
    async def pair(*args, **kwargs): return observed(liquidity_usd=0)
    monkeypatch.setattr(price_service, "_jup_get_price_info", forbidden)
    monkeypatch.setattr(price_service, "_jup_get_usd_price", forbidden)
    monkeypatch.setattr(dexscreener, "get_pair", pair)
    tick = await price_service.get_price(MINT, liquidity_only=True, force_refresh=True)
    assert fresh_market_value(tick, "liquidity_usd") == 0 and tick["price_usd"] is None


@pytest.mark.asyncio
async def test_critical_failure_invalidates_old_general_and_partial_snapshots(monkeypatch):
    key = f"price:{MINT}:0:1"
    simple_cache.cache_set(key, observed(price_usd=123), ttl=999)
    simple_cache.cache_set(key + ":partial", observed(price_usd=456), ttl=999)
    async def absent(*args, **kwargs): return None
    monkeypatch.setattr(price_service, "_jup_get_price_info", absent)
    monkeypatch.setattr(dexscreener, "get_pair", absent)
    assert await price_service.get_price(MINT, critical=True, price_only=True) is None
    assert simple_cache.cache_get(key) is simple_cache.cache_get(key + ":partial") is None


@pytest.mark.asyncio
async def test_freshness_is_rechecked_after_slow_full_snapshot_assembly(monkeypatch):
    async def info(*args, **kwargs):
        return jupiter_price.PriceInfo("OK", 2, True, 1, received_at=time.time())
    async def delayed_impact(tick, address):
        # Isolated elapsed-time simulation; no actual sleep or provider request.
        tick["market_observation"]["fields"]["price_usd"]["received_at"] -= 100
        return tick
    async def pair(*args, **kwargs): return observed(liquidity_usd=100)
    monkeypatch.setattr(price_service, "_jup_get_price_info", info)
    monkeypatch.setattr(price_service, "_attach_jupiter_impact", delayed_impact)
    monkeypatch.setattr(dexscreener, "get_pair", pair)
    assert await price_service.get_price(MINT, force_refresh=True) is None


@pytest.mark.asyncio
async def test_forced_gecko_bypasses_skip_cache_but_retains_hard_timeout_contract(monkeypatch):
    simple_cache.cache_set(f"price:gt_skip:{MINT}", True, ttl=999)
    calls = []
    async def absent(*args, **kwargs): return None
    async def gecko(network, address, **kwargs):
        calls.append(kwargs)
        return stamp_market_observation({"address": MINT, "liquidity_usd": 0}, "geckoterminal")
    monkeypatch.setattr(price_service, "USE_GECKO_TERMINAL", True)
    monkeypatch.setattr(dexscreener, "get_pair", absent)
    monkeypatch.setattr(price_service, "get_gt_data_async", gecko)
    tick = await price_service.get_price(MINT, liquidity_only=True, use_gt=True, force_refresh=True)
    assert fresh_market_value(tick, "liquidity_usd") == 0
    assert calls == [{"timeout": price_service._GT_TIMEOUT_S, "force_refresh": True}]


@pytest.mark.asyncio
async def test_jupiter_scalar_force_refresh_preserves_receipt_on_cache_read(monkeypatch):
    calls = []
    async def fetch(mints):
        calls.append(mints)
        return jupiter_price.parse_price_payload({MINT: {"usdPrice": 2}}, mints, received_at=time.time())
    monkeypatch.setattr(jupiter_price, "_fetch_batch_with_status", fetch)
    jupiter_price._cache_set_ok(MINT, 123)
    first = await jupiter_price.get_price(MINT, force_refresh=True)
    second = await jupiter_price.get_price(MINT)
    assert first.price_usd == second.price_usd == 2
    assert first.received_at == second.received_at and first.received_at is not None
    jupiter_price._cache_set_nil(MINT)
    assert await jupiter_price.get_usd_price(MINT, force_refresh=True) == 2
    assert calls == [[MINT], [MINT]]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 0, -1])
async def test_jupiter_batch_rejects_invalid_provider_values(monkeypatch, value):
    async def fetch(mints):
        return jupiter_price.PriceBatch({MINT: jupiter_price.PricePoint("OK", value),
                                        OTHER: jupiter_price.PricePoint("OK", 999)}, received_at=time.time())
    monkeypatch.setattr(jupiter_price, "_fetch_batch_with_status", fetch)
    result = await jupiter_price.get_many_prices([MINT], force_refresh=True)
    assert result[MINT].price_usd is None and OTHER not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["NIL", "ERR"])
async def test_failed_jupiter_refresh_cannot_resurrect_old_ok_cache(monkeypatch, status):
    async def fetch(mints):
        return jupiter_price.PriceBatch({MINT: jupiter_price.PricePoint(status)}, received_at=time.time())
    monkeypatch.setattr(jupiter_price, "_fetch_batch_with_status", fetch)
    jupiter_price._cache_set_ok(MINT, 123, received_at=time.time())
    result = await jupiter_price.get_price(MINT, force_refresh=True)
    assert result.price_usd is None
    assert jupiter_price._cache_get_ok(MINT) is None and MINT not in jupiter_price._ok_received_at


class AsyncResponse:
    status = 200
    def __init__(self, payload): self.payload = payload
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self, **kwargs): return deepcopy(self.payload)
    def raise_for_status(self): pass


class AsyncSession:
    def __init__(self, payload): self.payload = payload; self.calls = 0
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def get(self, *args, **kwargs):
        self.calls += 1
        return AsyncResponse(self.payload)
    async def close(self): pass


@pytest.mark.asyncio
async def test_real_dex_fetcher_bypasses_both_caches_and_returns_detached_receipts(monkeypatch):
    pair = {"baseToken": {"address": MINT}, "pairAddress": OTHER, "chainId": "solana",
        "priceUsd": "2", "liquidity": {"usd": 0}, "txns": {"m5": {"buys": 0, "sells": 0}}}
    client = AsyncSession({"pairs": [pair]})
    monkeypatch.setattr(dexscreener.aiohttp, "ClientSession", lambda *a, **k: client)
    simple_cache.cache_set(f"dex:{MINT}", dexscreener._SENTINEL_NIL, ttl=999)
    first = await dexscreener.get_pair(MINT, force_refresh=True)
    first["liquidity"]["usd"] = 999
    second = await dexscreener.get_pair(MINT)
    assert second["liquidity"]["usd"] == 0 and client.calls == 1
    assert first["market_observation"] == second["market_observation"]
    await dexscreener.get_pair(MINT, force_refresh=True)
    assert client.calls == 2


@pytest.mark.asyncio
async def test_real_dex_search_cannot_substitute_a_similarly_named_token(monkeypatch):
    client = AsyncSession({"pairs": [{"baseToken": {"address": OTHER}, "chainId": "solana",
        "priceUsd": "2", "liquidity": {"usd": 1e9}}]})
    monkeypatch.setattr(dexscreener.aiohttp, "ClientSession", lambda *a, **k: client)
    assert await dexscreener.get_pair(MINT, force_refresh=True) is None
    assert client.calls == 3


@pytest.mark.asyncio
async def test_real_birdeye_cache_retains_http_receipt_and_throttle(monkeypatch):
    client = AsyncSession({"success": True, "data": {"address": MINT, "priceUsd": "2", "liquidityUsd": 0, "tvlUsd": 999}})
    throttles = []
    async def throttle(): throttles.append(True)
    monkeypatch.setattr(birdeye, "_API_KEY", "synthetic")
    monkeypatch.setattr(birdeye, "_throttle", throttle)
    monkeypatch.setattr(birdeye.aiohttp, "ClientSession", lambda *a, **k: client)
    simple_cache.cache_set(f"be:v2:token:{MINT}", birdeye._SENTINEL_NIL, ttl=999)
    first = await birdeye.get_token_info(MINT, force_refresh=True)
    second = await birdeye.get_token_info(MINT)
    assert first["market_observation"] == second["market_observation"]
    assert second["liquidity_usd"] == 0 and len(throttles) == client.calls == 1
    await birdeye.get_token_info(MINT, force_refresh=True)
    assert len(throttles) == client.calls == 2


@pytest.mark.asyncio
async def test_real_gecko_async_force_keeps_provider_cooldown_and_rate_limiter(monkeypatch):
    client = AsyncSession({"data": {"attributes": {"price_usd": "2", "reserve_in_usd": 0}}})
    calls = []
    async def cooldown(): calls.append("cooldown")
    class Limiter:
        async def __aenter__(self): calls.append("limiter")
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(geckoterminal, "USE_GECKO_TERMINAL", True)
    monkeypatch.setattr(geckoterminal, "_wait_for_global_cooldown_async", cooldown)
    monkeypatch.setattr(geckoterminal, "GECKO_LIMITER", Limiter())
    monkeypatch.setattr(geckoterminal, "_min_interval_s", 0)
    simple_cache.cache_set(f"gt:solana:{MINT}", geckoterminal._SENTINEL_NIL, ttl=999)
    first = await geckoterminal.get_token_data_async("solana", MINT, client, force_refresh=True)
    first["liquidity"]["usd"] = 999
    second = await geckoterminal.get_token_data_async("solana", MINT, client)
    assert second["liquidity"]["usd"] == 0 and client.calls == 1
    await geckoterminal.get_token_data_async("solana", MINT, client, force_refresh=True)
    assert calls == ["cooldown", "limiter", "cooldown", "limiter"] and client.calls == 2


def test_real_gecko_sync_force_keeps_cooldown_and_rate_limiter(monkeypatch):
    calls = []
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"data": {"attributes": {"price_usd": "2", "reserve_in_usd": 0}}}
    class Client:
        def get(self, *args, **kwargs): calls.append("http"); return Response()
    monkeypatch.setattr(geckoterminal, "USE_GECKO_TERMINAL", True)
    monkeypatch.setattr(geckoterminal, "_wait_for_global_cooldown_sync", lambda: calls.append("cooldown"))
    monkeypatch.setattr(geckoterminal, "_throttle_internal", lambda: calls.append("throttle"))
    monkeypatch.setattr(geckoterminal, "_acquire_sync", lambda: calls.append("limiter"))
    simple_cache.cache_set(f"gt:solana:{MINT}", geckoterminal._SENTINEL_NIL, ttl=999)
    first = geckoterminal.get_token_data("solana", MINT, Client(), force_refresh=True)
    assert fresh_market_value(first, "liquidity_usd") == 0
    first["market_observation"]["fields"]["liquidity_usd"]["value"] = 999
    second = geckoterminal.get_token_data("solana", MINT, Client())
    assert fresh_market_value(second, "liquidity_usd") == 0
    assert calls == ["cooldown", "throttle", "limiter", "http", "cooldown"]


def test_provider_updated_time_is_not_fabricated_token_creation_time():
    assert birdeye._normalize_token_payload(MINT, {"updateUnixTime": time.time()})["created_at"] is None
    assert geckoterminal._normalize_attributes(MINT, {"updated_at": "2026-10-07T12:00:00Z"})["created_at"] is None
    missing = dexscreener._norm_from_pair({"baseToken": {"address": MINT}})
    assert missing["txns_last_5m"] is None


@pytest.mark.parametrize("current,expected", [(0, True), (10, True), (11, False),
    (None, False), (True, False), (-1, False), (float("nan"), False), (float("inf"), False)])
def test_known_zero_triggers_crush_but_unknown_liquidity_does_not(current, expected):
    assert liquidity_crushed(100, current, .1) is expected


def test_real_monitor_and_buy_gate_use_fresh_contract_without_cached_typeerror_downgrade():
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    monitor = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_check_positions")
    calls = [n for n in ast.walk(monitor) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "price_service"
             and n.func.attr in {"get_price", "get_price_usd"}]
    assert len(calls) == 7
    for call in calls:
        keywords = {k.arg: k.value for k in call.keywords}
        assert any(isinstance(keywords.get(key), ast.Constant) and keywords[key].value is True
                   for key in ("force_refresh", "critical"))
    assert not any(isinstance(n, ast.ExceptHandler) and isinstance(n.type, ast.Name)
                   and n.type.id == "TypeError" for n in ast.walk(monitor))
    buy = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_evaluate_and_buy")
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "get_jupiter_price_snapshot" for n in ast.walk(buy))
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "liquidity_crushed" for n in ast.walk(monitor))


@pytest.mark.asyncio
async def test_real_batch_preload_preserves_per_mint_receipts_and_later_expiry():
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_prefetch_batch_prices")
    received = time.time()
    async def batch(addresses, *, force_refresh):
        assert force_refresh is True
        return {MINT: SimpleNamespace(status="OK", price_usd=2, received_at=received),
                OTHER: SimpleNamespace(status="OK", price_usd=9, received_at=received - 121)}
    namespace = {"List": list, "Dict": dict, "math": math, "USE_JUPITER_PRICE": True,
        "jupiter_price": SimpleNamespace(get_many_prices=batch), "stamp_market_observation": stamp_market_observation,
        "fresh_market_value": fresh_market_value, "log": SimpleNamespace(debug=lambda *a: None, warning=lambda *a: None)}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "run_bot.py", "exec"), namespace)
    prices = await namespace["_prefetch_batch_prices"]([MINT, OTHER])
    assert set(prices) == {MINT}
    assert fresh_market_value(prices[MINT], "price_usd", now=received + 5) == 2
    assert fresh_market_value(prices[MINT], "price_usd", now=received + 31) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_batch", [False, True])
async def test_actual_monitor_price_branch_refreshes_expired_batch_and_keeps_fallback_origin(stale_batch):
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_check_positions")
    branch = next(n for n in ast.walk(function) if isinstance(n, ast.If)
                  and isinstance(n.test, ast.Name) and n.test.id == "prefer_dex")
    batch = stamp_market_observation({"address": MINT, "price_usd": 2}, "jupiter",
                                     received_at=time.time() - (121 if stale_batch else 0))
    calls = []
    async def get_price(address, **kwargs):
        calls.append(kwargs)
        return {**observed(price_usd=3, liquidity_usd=0), "price_source": "dexscreener"}
    namespace = {"prefer_dex": False, "batch_prices": {MINT: batch}, "mint_key": MINT,
        "price": None, "price_tick": None, "liquidity_tick": None, "price_src": None,
        "price_service": SimpleNamespace(get_price=get_price), "fresh_market_value": fresh_market_value,
        "batch_resolved": 0, "fallback_resolved": 0, "critical_resolved": 0, "dex_full_resolved": 0,
        "crit_used": 0, "_CRIT_MAX": 0, "_near_exit_zone": lambda *a: False}
    wrapper = ast.AsyncFunctionDef(name="run_branch", args=ast.arguments(posonlyargs=[], args=[],
        vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]), body=[ast.Global(names=[
            "price", "price_tick", "liquidity_tick", "price_src", "batch_resolved", "fallback_resolved",
            "critical_resolved", "dex_full_resolved", "crit_used"]), branch,
        ast.Return(value=ast.Tuple(elts=[ast.Name(id=n, ctx=ast.Load()) for n in
            ("price", "price_src", "price_tick")], ctx=ast.Load()))], decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "run_bot.py", "exec"), namespace)
    price, source, tick = await namespace["run_branch"]()
    assert price == (3 if stale_batch else 2)
    assert source == ("dexscreener" if stale_batch else "jup_batch")
    assert len(calls) == int(stale_batch)
    assert fresh_market_value(tick, "price_usd") == price
