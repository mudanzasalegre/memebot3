"""Synthetic native valuation projections; no market calls or profit claims."""
from copy import deepcopy
import datetime as dt
import time

import pytest

from fetcher import birdeye, dexscreener
from utils import price_service
from utils.market_observation import fresh_market_value, retain_fresh_market_fields, stamp_market_observation

MINT = "So11111111111111111111111111111111111111112"
PAIR = "11111111111111111111111111111111"


def project(provider, raw):
    data = {"address": MINT, "price": 2, "priceUsd": "2", "_market_received_at": time.time(),
            "chainId": "solana", "pairAddress": PAIR, "baseToken": {"address": MINT},
            "base": {"address": MINT}, **raw}
    if provider == "birdeye_token":
        return birdeye._normalize_token_payload(MINT, data)
    if provider == "birdeye_pool":
        return birdeye._normalize_pool_payload(PAIR, data)
    if provider == "dexscreener":
        return dexscreener._stamp_pair_observation(data)
    return price_service._coerce_tick_numbers(data)


@pytest.mark.parametrize("provider", ["birdeye_token", "birdeye_pool", "dexscreener", "common"])
@pytest.mark.parametrize("cap", [None, 0, 12000])
def test_cap_and_fdv_remain_independent(provider, cap):
    out = project(provider, {"marketCap": cap, "fdv": 99999})
    assert out["market_cap_usd"] == cap
    assert out["fdv_usd"] == 99999
    if provider != "common":
        assert out["fdv"] == 99999
        assert fresh_market_value(out, "market_cap_usd") == cap
        assert fresh_market_value(out, "fdv_usd") == 99999


@pytest.mark.parametrize("provider", ["birdeye_token", "birdeye_pool", "dexscreener", "common"])
@pytest.mark.parametrize("bad", [True, False, -1, float("nan"), float("inf"), {}, []])
def test_invalid_cap_does_not_borrow_fdv(provider, bad):
    out = project(provider, {"marketCap": bad, "fdv": 99999})
    assert out["market_cap_usd"] is None
    assert out["fdv_usd"] == 99999


@pytest.mark.parametrize("provider", ["birdeye_token", "birdeye_pool", "dexscreener", "common"])
def test_missing_fdv_is_not_invented_from_cap_or_supply(provider):
    out = project(provider, {"marketCap": 12000, "totalSupply": 123456789,
                             "circulatingSupply": 500, "supply": "1000000000000000"})
    assert out["market_cap_usd"] == 12000
    assert out["fdv_usd"] is None
    if provider != "common":
        assert out["fdv"] is None


@pytest.mark.parametrize("cap", [None, 0, 12000, True, -1])
def test_canonical_cap_is_authoritative_not_replaced_by_alias(cap):
    out = project("common", {"market_cap_usd": cap, "marketCap": 12345, "fdv": 99999})
    assert out["market_cap_usd"] == (cap if type(cap) in (int, float) and cap >= 0 else None)


@pytest.mark.parametrize("tail", [500, 1000, 25000, 1000000])
def test_extreme_momentum_survives_projection_receipt_and_merge(tail):
    incoming = project("birdeye_token", {"marketCap": None, "fdv": 99999,
                                         "priceChange5mPercent": tail})
    primary = stamp_market_observation({"address": MINT, "price_usd": 2}, "jupiter")
    out = price_service._merge_market_fields(primary, incoming, "birdeye")
    assert fresh_market_value(out, "price_pct_5m") == tail
    assert out["market_cap_usd"] is None and fresh_market_value(out, "fdv_usd") == 99999


def test_merge_keeps_zero_cap_fdv_and_chosen_field_receipts():
    original = stamp_market_observation({"address": MINT, "price_usd": 2,
        "market_cap_usd": 0, "fdv_usd": 100}, "dexscreener", received_at=time.time() - 2)
    later = stamp_market_observation({"address": MINT, "market_cap_usd": 999,
        "fdv_usd": 99999}, "birdeye")
    untouched = deepcopy(original)
    out = price_service._merge_market_fields(original, later, "birdeye")
    assert out["market_cap_usd"] == 0 and out["fdv_usd"] == 100
    assert fresh_market_value(out, "fdv_usd", source="dexscreener") == 100
    assert original == untouched


@pytest.mark.parametrize("field", ["market_cap_usd", "fdv_usd"])
def test_stale_valuation_aliases_cannot_resurrect_rejected_observations(field):
    tick = stamp_market_observation({"address": MINT, "market_cap_usd": 12000,
        "fdv_usd": 99999}, "dexscreener", received_at=time.time() - 120)
    tick.update({"marketCap": 12345, "marketCapUsd": 12345, "fdv": 99999,
                 "fdvUsd": 99999, "fullyDilutedValuation": 99999})
    out = price_service._coerce_tick_numbers(retain_fresh_market_fields(tick))
    assert out[field] is None
    assert all(alias not in out for alias in ("marketCap", "marketCapUsd", "fdv", "fdvUsd",
                                              "fullyDilutedValuation"))


@pytest.mark.parametrize("cap,expected", [(None, True), (0, False), (12000, True), (99999, False)])
def test_actual_basic_filter_never_substitutes_fdv_for_cap(monkeypatch, cap, expected):
    from analytics import filters
    monkeypatch.setattr(filters, "TRADING_HOURS", "")
    monkeypatch.setattr(filters, "TRADING_HOURS_EXTRA", "")
    monkeypatch.setattr(filters, "BLOCK_HOURS", "")
    monkeypatch.setattr(filters, "effective_thresholds", lambda token:
        filters.FilterThresholds("dex", 1, 10, 1000, 100, 5000, 20000))
    token = {"address":MINT, "chainId":"solana", "created_at":dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=6),
        "market_cap_usd":cap, "fdv":99999, "liquidity_usd":20000, "volume_24h_usd":50000,
        "holders":100, "txns_last_5m":200, "price_pct_5m":25000}
    assert filters.basic_filters(token) is expected


@pytest.mark.parametrize("cap", [None, 0])
def test_dynamic_bucket_does_not_rescue_old_buy_cap(cap):
    from runtime.dynamic_thresholds import _bucket_key
    from analytics.report_utils import mcap_bucket
    row = {"market_cap_usd":cap, "buy_market_cap_usd":12345}
    assert _bucket_key(row).split("|")[2] == mcap_bucket(cap)
