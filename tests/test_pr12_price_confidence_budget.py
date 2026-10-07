from __future__ import annotations

import pytest

from analytics import api_budget
from utils import price_service, simple_cache


@pytest.mark.asyncio
async def test_provider_rate_limit_pauses_entries_not_exits() -> None:
    api_budget.reset_provider_circuits()

    api_budget.record_provider_event("jupiter", "rate_limit", now=100.0, cooldown_s=60.0)

    entries_ok, entry_reason, entry_snapshot = api_budget.provider_entries_allowed(["jupiter"], now=120.0)
    exits_ok, exit_reason, exit_snapshot = api_budget.provider_exits_allowed(["jupiter"], now=120.0)

    assert entries_ok is False
    assert entry_reason == "provider_degraded:jupiter"
    assert entry_snapshot["providers"]["jupiter"]["degraded"] is True
    assert exits_ok is True
    assert exit_reason is None
    assert exit_snapshot["providers"]["jupiter"]["degraded"] is True

    entries_ok_after, _, _ = api_budget.provider_entries_allowed(["jupiter"], now=161.0)
    assert entries_ok_after is True


@pytest.mark.asyncio
async def test_no_price_partial_snapshot_marks_confidence(monkeypatch) -> None:
    simple_cache._CACHE.clear()
    address = "A" * 44

    async def no_jupiter_price(_address: str):
        return None

    async def no_dex_pair(_address: str):
        return None

    monkeypatch.setattr(price_service, "_jup_get_usd_price", no_jupiter_price)
    monkeypatch.setattr(price_service, "_USE_BIRDEYE", False)
    monkeypatch.setattr(price_service, "USE_GECKO_TERMINAL", False)
    monkeypatch.setattr(price_service, "_RETRY_ON_FAIL", 0)
    monkeypatch.setattr(price_service.dexscreener, "get_pair", no_dex_pair)

    snapshot = await price_service.get_price(address, price_only=True, allow_partial=True)

    assert snapshot is not None
    assert snapshot["address"] == address
    assert snapshot["price_usd"] is None
    assert snapshot["price_confidence"] == "none"
    assert snapshot["price_confidence_reason"] == "no_price"


@pytest.mark.asyncio
async def test_price_service_positive_and_negative_cache_ttl(monkeypatch) -> None:
    simple_cache._CACHE.clear()
    positive_address = "B" * 44
    negative_address = "C" * 44
    calls = {"jupiter": 0}

    async def jupiter_price_once(_address: str):
        calls["jupiter"] += 1
        return 0.123

    async def no_dex_pair(_address: str):
        return None

    monkeypatch.setattr(price_service, "_jup_get_usd_price", jupiter_price_once)
    monkeypatch.setattr(price_service, "_USE_BIRDEYE", False)
    monkeypatch.setattr(price_service, "USE_GECKO_TERMINAL", False)
    monkeypatch.setattr(price_service, "_RETRY_ON_FAIL", 0)
    monkeypatch.setattr(price_service.dexscreener, "get_pair", no_dex_pair)

    first = await price_service.get_price(positive_address, price_only=True)

    async def exploding_jupiter(_address: str):
        raise AssertionError("positive cache miss")

    monkeypatch.setattr(price_service, "_jup_get_usd_price", exploding_jupiter)
    second = await price_service.get_price(positive_address, price_only=True)

    assert first is not None
    assert second is not None
    assert first["price_usd"] == pytest.approx(0.123)
    assert second["price_usd"] == pytest.approx(0.123)
    assert second["price_confidence"] == "high"
    assert calls["jupiter"] == 1

    async def no_jupiter_price(_address: str):
        return None

    monkeypatch.setattr(price_service, "_jup_get_usd_price", no_jupiter_price)
    miss = await price_service.get_price(negative_address, price_only=True)

    async def exploding_query(*_args, **_kwargs):
        raise AssertionError("negative cache miss")

    monkeypatch.setattr(price_service, "_query_sources", exploding_query)
    cached_miss = await price_service.get_price(negative_address, price_only=True)

    assert miss is None
    assert cached_miss is None
