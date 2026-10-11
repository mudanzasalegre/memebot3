"""Current documented contracts with fake HTTP only; no access/profit proof."""
import asyncio
from copy import deepcopy
import datetime as dt
import math
import time

import pytest

from fetcher import birdeye as be
from analytics.token_time import compute_age_minutes
from utils import price_service, simple_cache
from utils.market_observation import fresh_market_value

MINT = "So11111111111111111111111111111111111111112"
OTHER = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAIR = "11111111111111111111111111111111"
OVERVIEW = "/defi/token_overview"
POOL = "/defi/v3/pair/overview/single"
CREATION = "/defi/token_creation_info"


class Response:
    def __init__(self, payload, status=200): self.payload, self.status = payload, status
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self):
        if isinstance(self.payload, BaseException): raise self.payload
        return deepcopy(self.payload)


class Session:
    def __init__(self, response): self.response, self.calls = response, []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(be, "_API_KEY", "synthetic-not-a-credential")
    async def throttle(): pass
    monkeypatch.setattr(be, "_throttle", throttle)
    be._fail_count.clear()
    def forbidden(*args, **kwargs): raise AssertionError("Real HTTP forbidden")
    monkeypatch.setattr(be.aiohttp, "ClientSession", forbidden)
    for kind, address in (("token", MINT), ("pool", PAIR), ("creation", MINT)):
        for prefix in ("be", "be:v2"):
            simple_cache.cache_delete(f"{prefix}:{kind}:{address}")


def client(monkeypatch, data, *, success=True, status=200):
    session = Session(Response({"success": success, "data": data}, status))
    monkeypatch.setattr(be.aiohttp, "ClientSession", lambda *a, **k: session)
    return session


def overview(**extra):
    return {"address": MINT, "price": 2, "liquidity": 5000, "v24hUSD": 1000,
        "marketCap": 12000, "fdv": 99999, "trade5m": 10, "buy5m": 7,
        "sell5m": 3, "holder": 20, "priceChange1mPercent": -1,
        "priceChange5mPercent": 25000, "v5mChangePercent": 1000, **extra}


def creation(**extra):
    return {"tokenAddress": MINT, "blockUnixTime": 1697044029,
        "blockHumanTime": "2023-10-11T17:07:09.000Z", "slot": 223012712,
        "txHash": "1" * 64, **extra}


@pytest.mark.asyncio
async def test_current_overview_request_and_flat_activity(monkeypatch):
    session = client(monkeypatch, overview())
    result = await be.get_token_info(MINT, force_refresh=True)
    assert result is not None
    url, options = session.calls[0]
    assert url == "https://public-api.birdeye.so" + OVERVIEW
    assert options["params"] == {"address": MINT}
    assert options["headers"]["X-API-KEY"] == "synthetic-not-a-credential"
    assert options["headers"]["x-chain"] == "solana"
    assert "Authorization" not in options["headers"]
    assert options["allow_redirects"] is False
    for field, expected in {"price_usd": 2, "liquidity_usd": 5000, "volume_24h_usd": 1000,
            "market_cap_usd": 12000, "txns_last_5m": 10, "txns_last_5m_buys": 7,
            "txns_last_5m_sells": 3, "holders": 20, "price_pct_1m": -1,
            "price_pct_5m": 25000, "volume_pct_5m": 1000}.items():
        assert fresh_market_value(result, field, source="birdeye") == expected
    assert result["created_at"] is None and len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "solana" * 8, "0" * 44, "1" * 31, "1" * 33,
    "0x" + "a" * 40, {"address": MINT}, 123])
async def test_invalid_public_keys_never_reach_http(monkeypatch, bad):
    session = client(monkeypatch, overview())
    assert await be.get_token_info(bad, force_refresh=True) is None
    assert await be.get_pool_info(bad, force_refresh=True) is None
    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{"address": OTHER}, {"price": 2}, {"address": MINT.lower()},
    {"address": MINT, "chainId": "ethereum"}, {"address": MINT, "network": "base"}])
async def test_wrong_or_missing_identity_is_not_relabelled(monkeypatch, data):
    session = client(monkeypatch, data)
    assert await be.get_token_info(MINT, force_refresh=True) is None
    assert await be.get_token_info(MINT) is None
    assert len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, None, 1, "true"])
async def test_strict_success_envelope(monkeypatch, success):
    client(monkeypatch, {"address": MINT, "priceUsd": 2}, success=success)
    assert await be.get_token_info(MINT, force_refresh=True) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500])
async def test_http_failure_is_negative_cached(monkeypatch, status):
    session = client(monkeypatch, overview(), status=status)
    assert await be.get_token_info(MINT, force_refresh=True) is None
    assert await be.get_token_info(MINT) is None
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_cache_keeps_original_receipt_and_detaches_values(monkeypatch):
    session = client(monkeypatch, overview(liquidity=0, v24hUSD=0, holder=0, buy5m=0))
    first = await be.get_token_info(MINT, force_refresh=True)
    first["market_observation"]["fields"]["price_usd"]["value"] = 99
    second = await be.get_token_info(MINT)
    assert second["market_observation"]["fields"]["price_usd"]["value"] == 2
    assert second["liquidity_usd"] == second["volume_24h_usd"] == second["holders"] == 0
    assert second["market_observation"]["fields"]["price_usd"]["received_at"] == first["market_observation"]["fields"]["price_usd"]["received_at"]
    assert len(session.calls) == 1


@pytest.mark.parametrize("node", [True, [], 8, "malformed"])
def test_malformed_optional_nodes_cannot_crash_normalization(node):
    raw = overview(priceInfo=node, volume=node)
    result = be._normalize_token_payload(MINT, raw)
    assert result["price_usd"] == 2 and result["liquidity_usd"] == 5000
    assert raw["liquidity"] == 5000


@pytest.mark.parametrize("field", ["created_at", "createdAt", "created", "createdAtUtc",
    "createTime", "createUnixTime", "age_minutes", "age_min", "token_age_min"])
def test_overview_aliases_do_not_authenticate_birth(field):
    raw = overview(liquidity={"usd": 5000}, **{field: "2023-10-11T17:07:09Z" if "age" not in field else 0})
    result = be._normalize_token_payload(MINT, raw)
    assert result["created_at"] is None and compute_age_minutes(result) is None


@pytest.mark.asyncio
async def test_current_pair_contract_keeps_pair_and_mint_grains(monkeypatch):
    session = client(monkeypatch, {"address": PAIR, "base": {"address": MINT, "symbol": "SOL"},
        "quote": {"address": OTHER}, "created_at": "2023-02-28T03:00:02.253Z",
        "price": 2, "liquidity": 3000, "volume_24h": 4000})
    result = await be.get_pool_info(PAIR, force_refresh=True)
    assert result is not None and result["address"] == MINT and result["pair_address"] == PAIR
    assert session.calls[0][0].endswith(POOL)
    assert result["created_at"] is None and result["pair_created_at"].year == 2023
    assert result["liquidity_usd"] == 3000 and result["volume_24h_usd"] == 4000
    assert result["market_observation"]["address"] == MINT


@pytest.mark.asyncio
async def test_creation_request_is_separate_from_market_receipt(monkeypatch):
    session = client(monkeypatch, creation())
    result = await be.get_token_creation_info(MINT, force_refresh=True)
    assert result["created_at"] == dt.datetime(2023, 10, 11, 17, 7, 9, tzinfo=dt.timezone.utc)
    assert session.calls[0][0].endswith(CREATION)
    assert session.calls[0][1]["params"] == {"address": MINT}
    assert "market_observation" not in result
    proof = result["token_birth_observation"]
    assert proof["address"] == MINT and proof["source"] == "birdeye"
    assert proof["basis"] == "provider_reported_mint_creation_not_independent_chain_verification"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [{"tokenAddress": OTHER}, {"blockUnixTime": True},
    {"blockUnixTime": -1}, {"blockUnixTime": math.inf}, {"blockUnixTime": time.time() + 3600},
    {"blockUnixTime": 1697044029000}, {"slot": True}, {"txHash": "invalid"},
    {"blockHumanTime": "2023-10-12T17:07:09.000Z"}])
async def test_invalid_creation_context_cannot_be_birth(monkeypatch, mutation):
    client(monkeypatch, creation(**mutation))
    assert await be.get_token_creation_info(MINT, force_refresh=True) is None


@pytest.mark.asyncio
async def test_cancellation_propagates_without_negative_cache(monkeypatch):
    session = Session(Response(asyncio.CancelledError()))
    monkeypatch.setattr(be.aiohttp, "ClientSession", lambda *a, **k: session)
    with pytest.raises(asyncio.CancelledError):
        await be.get_token_info(MINT, force_refresh=True)
    assert not any(MINT in key for key in be._fail_count)


@pytest.mark.asyncio
async def test_native_entry_collection_uses_current_overview_once(monkeypatch):
    session = client(monkeypatch, overview())
    monkeypatch.setattr(price_service, "_USE_BIRDEYE", True)
    monkeypatch.setattr(price_service, "_USE_JUPITER_PRICE", False)
    async def forbidden(*args, **kwargs): raise AssertionError("Unexpected provider fallback")
    monkeypatch.setattr(price_service.dexscreener, "get_pair", forbidden)
    result = await price_service.get_entry_snapshot(MINT, use_gt=False)
    assert result["price_pct_5m"] == 25000 and result["txns_last_5m_buys"] == 7
    assert fresh_market_value(result, "liquidity_usd") == 5000 and len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], True, "data", {"success": True},
    {"success": True, "data": []}, {"success": True, "data": {}}])
async def test_malformed_envelope_never_crashes_or_creates_a_receipt(monkeypatch, payload):
    session = Session(Response(payload))
    monkeypatch.setattr(be.aiohttp, "ClientSession", lambda *a, **k: session)
    assert await be.get_token_info(MINT, force_refresh=True) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("base", [None, [], True, {"address": "1" * 31}, {"address": "0" * 44}])
async def test_pair_requires_real_base_identity(monkeypatch, base):
    client(monkeypatch, {"address": PAIR, "base": base, "price": 2})
    assert await be.get_pool_info(PAIR, force_refresh=True) is None


@pytest.mark.asyncio
async def test_pair_identity_cannot_be_substituted(monkeypatch):
    client(monkeypatch, {"address": OTHER, "base": {"address": MINT}, "price": 2})
    assert await be.get_pool_info(PAIR, force_refresh=True) is None


@pytest.mark.parametrize("value", [None, True, -1, math.nan, math.inf, "unknown"])
def test_invalid_optional_counts_are_not_fabricated_zero(value):
    result = be._normalize_token_payload(MINT, overview(buy5m=value, holder=value))
    assert result["txns_last_5m_buys"] is None and result["holders"] is None


def test_current_metrics_are_unclipped_and_optional_absence_remains_unknown():
    result = be._normalize_token_payload(MINT, {"address": MINT, "price": 2,
        "liquidity": 0, "priceChange5mPercent": 1000000})
    assert result["price_pct_5m"] == 1000000 and result["liquidity_usd"] == 0
    assert result["txns_last_5m"] is result["holders"] is result["volume_24h_usd"] is None


@pytest.mark.asyncio
async def test_creation_cache_is_detached_and_retains_its_original_receipt(monkeypatch):
    session = client(monkeypatch, creation())
    first = await be.get_token_creation_info(MINT, force_refresh=True)
    received = first["token_birth_observation"]["received_at"]
    first["token_birth_observation"]["received_at"] = 1
    second = await be.get_token_creation_info(MINT)
    assert second["token_birth_observation"]["received_at"] == received and len(session.calls) == 1


@pytest.mark.asyncio
async def test_cache_cannot_relabel_other_identity_or_accept_legacy_generation(monkeypatch):
    simple_cache.cache_set(f"be:token:{MINT}", {"address": OTHER, "price": 99}, ttl=999)
    simple_cache.cache_set(f"be:v2:token:{MINT}", {"address": OTHER, "price": 99}, ttl=999)
    session = client(monkeypatch, overview())
    result = await be.get_token_info(MINT)
    assert result["address"] == MINT and result["price_usd"] == 2 and len(session.calls) == 1


@pytest.mark.asyncio
async def test_no_key_never_reaches_http_even_with_old_cache(monkeypatch):
    monkeypatch.setattr(be, "_API_KEY", None)
    session = client(monkeypatch, overview())
    assert await be.get_token_info(MINT) is None
    assert await be.get_pool_info(PAIR) is None
    assert await be.get_token_creation_info(MINT) is None
    assert session.calls == []


@pytest.mark.asyncio
async def test_missing_base58_dependency_does_not_weaken_identity_validation(monkeypatch):
    from utils import solana_addr
    monkeypatch.setattr(solana_addr, "_BASE58_IMPORT_OK", False)
    solana_addr._NORMALIZE_CACHE.clear()
    session = client(monkeypatch, overview())
    assert await be.get_token_info("0" * 44) is None
    assert await be.get_token_info(MINT + "_pump", force_refresh=True) is not None
    assert session.calls[0][1]["params"]["address"] == MINT


@pytest.mark.asyncio
async def test_force_refresh_still_uses_cooperative_throttle(monkeypatch):
    calls = []
    async def throttle(): calls.append(True)
    monkeypatch.setattr(be, "_throttle", throttle)
    session = client(monkeypatch, overview())
    await be.get_token_info(MINT, force_refresh=True)
    await be.get_token_info(MINT)
    await be.get_token_info(MINT, force_refresh=True)
    assert len(calls) == len(session.calls) == 2
