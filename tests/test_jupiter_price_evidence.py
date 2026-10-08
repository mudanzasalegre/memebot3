"""Synthetic Price V3 quality checks; no provider, wallet, orders or daemon."""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from fetcher import jupiter_price as price
from fetcher import jupiter_price_v3 as contract
from utils import price_service

TOKEN = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
OTHER = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def point(**overrides):
    return {"usdPrice": 2.0, "blockId": 123, "decimals": 6, **overrides}


def test_canonical_price_point_retains_units_not_route_or_market_asof():
    batch = contract.parse_price_payload({TOKEN: point()}, [TOKEN, OTHER], received_at=100.)
    assert batch == {TOKEN: ("OK", 2.), OTHER: ("NIL", None)}
    assert batch.received_at == 100.
    assert batch.points[TOKEN].block_id == 123 and batch.points[TOKEN].decimals == 6
    assert batch.points[OTHER].reason == "provider_omitted_price"


@pytest.mark.parametrize("payload", [None, [], 0, True, "{}", {"data": {}},
    {"error": "quota"}, {"message": "unavailable"}, {OTHER: point()}])
def test_invalid_response_map_is_unknown_not_negative(payload):
    batch = contract.parse_price_payload(payload, [TOKEN], received_at=100.)
    assert batch[TOKEN] == ("ERR", None)


@pytest.mark.parametrize("entry", [None, {}, [], True, 2, {"price": 2},
    *[point(usdPrice=value) for value in [None, True, 0, -1, float("nan"), float("inf"), "2", {}, []]]])
def test_present_invalid_price_is_unknown_never_official_omission(entry):
    batch = contract.parse_price_payload({TOKEN: entry}, [TOKEN, OTHER], received_at=100.)
    assert batch[TOKEN] == ("ERR", None)
    assert batch[OTHER] == ("NIL", None)


@pytest.mark.parametrize("bad", [{"blockId": True}, {"blockId": -1}, {"blockId": 2**64},
    {"blockId": "123"}, {"blockId": None}, {"decimals": True}, {"decimals": -1},
    {"decimals": 256}, {"decimals": 6.0}, {"decimals": None}, {"error": None}])
def test_invalid_provided_metadata_does_not_become_a_usable_point(bad):
    assert contract.parse_price_payload({TOKEN: point(**bad)}, [TOKEN])[TOKEN] == ("ERR", None)


@pytest.mark.parametrize("receipt", [True, 0, -1, "100", float("nan"), float("inf")])
def test_invalid_receipt_clock_cannot_freshen_price(receipt):
    assert contract.parse_price_payload({TOKEN: point()}, [TOKEN], received_at=receipt)[TOKEN] == ("ERR", None)


def test_optional_metadata_is_explicitly_unknown_not_invented():
    batch = contract.parse_price_payload({TOKEN: {"usdPrice": 2}}, [TOKEN])
    assert batch[TOKEN] == ("OK", 2.)
    assert batch.points[TOKEN].block_id is batch.points[TOKEN].decimals is None
    assert batch.received_at is None


@pytest.mark.parametrize("body", [b"", b"null", b"[{}]", b"{", b"\xff",
    ('{"' + TOKEN + '":{"usdPrice":NaN}}').encode(),
    ('{"' + TOKEN + '":{"usdPrice":2,"usdPrice":3}}').encode(),
    ('{"' + TOKEN + '":{"usdPrice":2},"' + TOKEN + '":{"usdPrice":3}}').encode(),
    b" " * (contract.MAX_PRICE_BODY_BYTES + 1)],
    ids=["empty", "null", "list", "truncated", "invalid_utf8", "nan", "duplicate_field", "duplicate_mint", "oversize"])
def test_bad_json_and_duplicate_keys_cannot_poison_nil_cache(body):
    assert contract.decode_price_body(body, [TOKEN], received_at=100.)[TOKEN] == ("ERR", None)


class Response:
    def __init__(self, value=None, *, body=None, status=200, headers=None, exit_hook=None):
        self.body = json.dumps(value).encode() if body is None else body
        self.status, self.headers = status, headers or {}
        self.content, self.offset, self.exit_hook = self, 0, exit_hook
        self.read_sizes = []

    async def __aenter__(self): return self
    async def __aexit__(self, *args):
        if self.exit_hook: self.exit_hook()
    async def read(self, size):
        self.read_sizes.append(size)
        part = self.body[self.offset:self.offset + size]
        self.offset += len(part)
        return part
    async def json(self, **kwargs):
        pytest.fail("unbounded JSON reader is forbidden")


@pytest.fixture
def http(monkeypatch):
    replies, calls = [], []
    class Session:
        closed = False
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            reply = replies.pop(0)
            if isinstance(reply, BaseException): raise reply
            return reply
    monkeypatch.setattr(price, "_ensure_session", AsyncMock(return_value=Session()))
    monkeypatch.setattr(price, "_throttle", AsyncMock())
    monkeypatch.setattr(price.jupiter_access, "acquire", AsyncMock())
    monkeypatch.setattr(price.jupiter_access, "observe", lambda *a, **kw: None)
    monkeypatch.setattr(price, "JUPITER_PRICE_URL", "https://api.jup.ag/price/v3")
    monkeypatch.setattr(price, "_JUP_API_KEY", "synthetic-key")
    price.clear_caches()
    yield replies, calls
    price.clear_caches()


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["_fetch_batch", "_fetch_batch_with_status"])
async def test_both_actual_price_readers_use_checked_bounded_transport(http, reader):
    replies, calls = http
    response = Response({TOKEN: point()})
    replies.append(response)
    result = await getattr(price, reader)([TOKEN])
    assert result[TOKEN] == (2. if reader == "_fetch_batch" else ("OK", 2.))
    assert len(calls) == 1 and calls[0][1]["allow_redirects"] is False
    assert response.read_sizes and max(response.read_sizes) <= 65536


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [400, 401, 429, 500, "shape", "point", "duplicate", "huge", "length"])
async def test_http_faults_remain_unknown_and_allow_next_price_opportunity(http, fault):
    replies, calls = http
    if isinstance(fault, int): response = Response({}, status=fault)
    elif fault == "shape": response = Response({"data": {}})
    elif fault == "point": response = Response({TOKEN: {"usdPrice": True}})
    elif fault == "duplicate": response = Response(body=('{"' + TOKEN + '":{},"' + TOKEN + '":{}}').encode())
    elif fault == "huge": response = Response(body=b" " * (contract.MAX_PRICE_BODY_BYTES + 100))
    else: response = Response({}, headers={"Content-Length": str(contract.MAX_PRICE_BODY_BYTES + 1)})
    replies.extend([response, Response({TOKEN: point()})])
    failed = await price.get_price(TOKEN)
    assert failed.status == "ERR" and TOKEN not in price._nil_cache and TOKEN not in price._ok_cache
    assert failed.has_route is failed.routes_count is None
    recovered = await price.get_price(TOKEN)
    assert recovered.status == "OK" and recovered.price_usd == 2 and len(calls) == 2
    assert response.offset <= contract.MAX_PRICE_BODY_BYTES + 1


@pytest.mark.asyncio
async def test_official_omission_alone_is_negatively_cached_and_force_refresh_can_recover(http):
    replies, calls = http
    replies.append(Response({}))
    first = await price.get_price(TOKEN)
    second = await price.get_price(TOKEN)
    assert first.status == second.status == "NIL"
    assert first.received_at == second.received_at and first.received_at is not None
    assert first.evidence_kind == second.evidence_kind == "http_omission"
    assert TOKEN in price._nil_cache and len(calls) == 1
    replies.append(Response({TOKEN: point()}))
    info = await price.get_price(TOKEN, force_refresh=True)
    assert info.status == "OK" and TOKEN not in price._nil_cache and TOKEN not in price._nil_backoff


@pytest.mark.asyncio
async def test_receipt_is_from_http_body_not_delayed_teardown_or_cache_read(http, monkeypatch):
    replies, calls = http
    clock = [100.]
    monkeypatch.setattr(price.time, "time", lambda: clock[0])
    replies.append(Response({TOKEN: point()}, exit_hook=lambda: clock.__setitem__(0, 200.)))
    first = await price.get_price(TOKEN, force_refresh=True)
    second = await price.get_price(TOKEN)
    assert first.received_at == second.received_at == 100. and len(calls) == 1
    assert second.block_id == 123 and second.decimals == 6
    assert second.has_route is second.routes_count is None and not second.market_asof_verified
    assert second.evidence_kind == "http_price"
    clock[0] = 201.
    monkeypatch.setattr(price_service, "_USE_JUPITER_PRICE", True)
    monkeypatch.setattr(price_service, "_jup_get_price_info", AsyncMock(return_value=second))
    assert await price_service.get_jupiter_price_snapshot(TOKEN) is None


@pytest.mark.asyncio
async def test_fresh_snapshot_retains_metadata_without_certifying_market_recency(http, monkeypatch):
    replies, _ = http
    replies.append(Response({TOKEN: point()}))
    monkeypatch.setattr(price_service, "_USE_JUPITER_PRICE", True)
    monkeypatch.setattr(price_service, "_jup_get_price_info", price.get_price)
    snapshot = await price_service.get_jupiter_price_snapshot(TOKEN)
    assert snapshot["price_usd"] == 2
    assert snapshot["jupiter_price_evidence"] == {"block_id": 123, "decimals": 6,
        "evidence_kind": "http_price", "market_asof_verified": False, "has_route": None}


@pytest.mark.asyncio
async def test_stable_shortcuts_and_deprecated_quote_alias_never_manufacture_route_truth(http):
    _, calls = http
    info = await price.get_price(OTHER)
    assert info.price_usd == 1 and info.received_at is None and info.confidence == "assumed"
    assert info.has_route is info.routes_count is None and info.evidence_kind == "constant_shortcut"
    alias = await price.get_quote_status(OTHER)
    assert alias["has_route"] is alias["routes_count"] is None
    assert alias["market_asof_verified"] is False and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.TimeoutError(), asyncio.CancelledError()])
async def test_timeout_is_unknown_but_cancellation_propagates(http, failure):
    replies, calls = http
    replies.append(failure)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError): await price.get_price(TOKEN)
    else:
        assert (await price.get_price(TOKEN)).status == "ERR"
    assert TOKEN not in price._nil_cache and len(calls) == 1


@pytest.mark.asyncio
async def test_untyped_adapter_values_cannot_be_restamped_as_http_evidence(http, monkeypatch):
    monkeypatch.setattr(price, "_fetch_batch_with_status", AsyncMock(return_value={TOKEN: ("OK", 2.)}))
    result = await price.get_price(TOKEN, force_refresh=True)
    assert result.status == "ERR" and result.received_at is None and TOKEN not in price._ok_cache


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [[TOKEN, TOKEN], ["not-a-mint"], [TOKEN] * 51])
async def test_invalid_batches_are_unknown_without_spending_provider_budget(http, requested):
    _, calls = http
    result = await price._fetch_batch_with_status(requested)
    assert all(status == "ERR" for status, _ in result.values()) and not calls


@pytest.mark.asyncio
async def test_expired_or_force_failed_prices_clear_original_metadata(http, monkeypatch):
    replies, _ = http
    replies.append(Response({TOKEN: point()}))
    assert (await price.get_price(TOKEN)).decimals == 6
    replies.append(Response({"error": "unavailable"}))
    assert (await price.get_price(TOKEN, force_refresh=True)).status == "ERR"
    assert TOKEN not in price._ok_points and TOKEN not in price._ok_received_at
    price._cache_set_ok(TOKEN, 2, received_at=time.time(), point=contract.PricePoint("OK", 2, 123, 6))
    monkeypatch.setattr(price, "_now", lambda: price._ok_cache[TOKEN][1] + 1)
    assert price._cache_get_ok(TOKEN) is None and TOKEN not in price._ok_points
