import asyncio
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import utils.price_service as price_service
from utils import simple_cache


@pytest.mark.asyncio
async def test_gecko_fallback_has_hard_timeout(monkeypatch):
    address = "A" * 44
    simple_cache._CACHE.clear()

    monkeypatch.setattr(price_service, "USE_GECKO_TERMINAL", True)
    monkeypatch.setattr(price_service, "_GT_TIMEOUT_S", 30.0)
    monkeypatch.setattr(price_service, "_GT_HARD_TIMEOUT_S", 0.05)
    monkeypatch.setattr(price_service, "_USE_BIRDEYE", False)
    monkeypatch.setattr(price_service, "_jup_get_usd_price", None)

    async def no_dex_pair(_address):
        return None

    async def slow_gecko(*_args, **_kwargs):
        await asyncio.sleep(10.0)
        return {"address": address, "price_usd": 1.0}

    monkeypatch.setattr(price_service.dexscreener, "get_pair", no_dex_pair)
    monkeypatch.setattr(price_service, "get_gt_data_async", slow_gecko)

    started = time.monotonic()
    out = await price_service._query_sources(address, use_gt=True, fields_needed=("price_usd",))
    elapsed = time.monotonic() - started

    assert out is None
    assert elapsed < 1.0
    assert simple_cache.cache_get(f"price:gt_skip:{address}") is True
