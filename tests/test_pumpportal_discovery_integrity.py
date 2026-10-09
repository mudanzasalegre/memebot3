"""Original discovery clocks and FIFO delivery, with no WebSocket/provider run."""
from copy import deepcopy
import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from fetcher import pumpfun

MINT = "So11111111111111111111111111111111111111112"


@pytest.fixture(autouse=True)
def isolated_clock(monkeypatch):
    now = [dt.datetime(2026, 10, 9, 2, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(pumpfun, "utc_now", lambda: now[0])
    monkeypatch.setattr(pumpfun, "_BUFFER_MAX", 2)
    monkeypatch.setattr(pumpfun, "_WINDOW_MIN", 60.)
    monkeypatch.setattr(pumpfun, "_SEEN_TTL_MIN", 10.)
    pumpfun._buffer.clear()
    pumpfun._seen.clear()
    if hasattr(pumpfun, "_pending"):
        pumpfun._pending.clear()
    yield now
    pumpfun._buffer.clear()
    pumpfun._seen.clear()
    if hasattr(pumpfun, "_pending"):
        pumpfun._pending.clear()


@pytest.mark.parametrize("stamp", [None, "not-a-date", True, False, float("nan"), float("inf"), -1, 0])
def test_absent_or_invalid_creation_clock_is_not_fabricated(stamp, isolated_clock):
    token = pumpfun._parse_event({"mint": MINT, "createdAt": stamp})
    assert token is not None
    assert token["created_at"] is None
    assert token["age_minutes"] is None and token["age_min"] is None
    assert token["discovered_at"] == isolated_clock[0]


def test_future_creation_clock_is_unknown_not_a_zero_age_birth(isolated_clock):
    token = pumpfun._parse_event({"mint": MINT, "createdAt": (isolated_clock[0] + dt.timedelta(seconds=1)).isoformat()})
    assert token["created_at"] is None and token["age_minutes"] is None
    assert token["discovered_at"] == isolated_clock[0]


def test_generic_event_clock_is_not_token_creation(isolated_clock):
    stamp = isolated_clock[0] - dt.timedelta(seconds=12)
    token = pumpfun._parse_event({"mint": MINT, "timestamp": stamp.timestamp()})
    assert token["created_at"] is None and token["age_minutes"] is None
    assert token["pumpportal_event_at"] == stamp
    assert token["discovered_at"] == isolated_clock[0]


def test_explicit_creation_clock_is_not_overwritten_by_a_generic_timestamp(isolated_clock):
    created = isolated_clock[0] - dt.timedelta(minutes=3)
    event = isolated_clock[0] - dt.timedelta(seconds=1)
    token = pumpfun._parse_event({"mint": MINT, "createdAt": created.isoformat(), "timestamp": event.timestamp()})
    assert token["created_at"] == created
    assert token["pumpportal_event_at"] == event
    assert token["discovered_at"] == isolated_clock[0]


@pytest.mark.parametrize("factor", [1, 1000, 1000000, 1000000000])
def test_numeric_clock_units_preserve_the_original_time(factor, isolated_clock):
    stamp = isolated_clock[0] - dt.timedelta(seconds=5)
    assert pumpfun._to_dt(int(stamp.timestamp()) * factor) == stamp


def test_never_delivered_overflow_event_can_be_received_again(isolated_clock):
    for address in ("A", "B", "C"):
        assert pumpfun._enqueue_event({"address": address, "created_at": isolated_clock[0]})
    assert [row["address"] for row in pumpfun._drain_events(2)] == ["B", "C"]
    assert pumpfun._enqueue_event({"address": "A", "created_at": isolated_clock[0]})
    assert pumpfun._drain_events(1)[0]["address"] == "A"


def test_pending_event_cannot_duplicate_after_seen_ttl_but_before_delivery(isolated_clock):
    token = {"address": "A", "created_at": isolated_clock[0]}
    assert pumpfun._enqueue_event(token)
    isolated_clock[0] += dt.timedelta(minutes=11)
    assert pumpfun._enqueue_event(token) is False
    assert [row["address"] for row in pumpfun._drain_events(2)] == ["A"]


def test_delivery_dedup_ttl_starts_at_actual_handoff(isolated_clock):
    token = {"address": "A", "created_at": isolated_clock[0]}
    assert pumpfun._enqueue_event(token)
    isolated_clock[0] += dt.timedelta(minutes=9)
    assert pumpfun._drain_events(1)
    isolated_clock[0] += dt.timedelta(minutes=2)
    assert pumpfun._enqueue_event(token) is False


def test_unknown_creation_expires_from_original_receipt_not_a_sliding_clock(isolated_clock):
    token = pumpfun._parse_event({"mint": MINT})
    assert pumpfun._enqueue_event(token)
    isolated_clock[0] += dt.timedelta(minutes=61)
    assert pumpfun._drain_events(1) == []
    assert token["created_at"] is None


def test_queue_detaches_discovery_input_before_caller_mutation(isolated_clock):
    token = {"address": "A", "created_at": isolated_clock[0], "discovered_at": isolated_clock[0], "name": "original"}
    original = deepcopy(token)
    assert pumpfun._enqueue_event(token)
    token.update(address="B", name="changed", created_at=isolated_clock[0] + dt.timedelta(days=1))
    assert pumpfun._drain_events(1)[0] == original


def test_original_discovery_context_survives_the_common_entry_boundary(isolated_clock):
    from runtime.entry_observation import discovery_candidate
    token = pumpfun._parse_event({"mint": MINT, "timestamp": isolated_clock[0].timestamp()})
    context = discovery_candidate(token)
    assert context["created_at"] is None
    assert context["discovered_at"] == isolated_clock[0]
    assert context["pumpportal_event_at"] == isolated_clock[0]


@pytest.mark.parametrize("value", [None, True, False, float("nan"), float("inf"), -1, 0, {}, [], "bad"])
def test_invalid_clock_parser_is_pure_and_returns_unknown(value):
    assert pumpfun._to_dt(value) is None


@pytest.mark.parametrize("invalid", [None, False, 0, "bad"])
def test_invalid_explicit_creation_is_not_resurrected_from_an_alias(invalid, isolated_clock):
    token = pumpfun._parse_event({"mint": MINT, "created_at": invalid, "createdAt": isolated_clock[0].isoformat()})
    assert token["created_at"] is None and token["age_min"] is None
    assert token["pumpportal_created_at_basis"] == "unknown"


def test_nested_explicit_creation_and_event_clocks_remain_distinct(isolated_clock):
    created = isolated_clock[0] - dt.timedelta(minutes=3)
    event = isolated_clock[0] - dt.timedelta(seconds=2)
    token = pumpfun._parse_event({"data": {"mint": MINT, "createdAt": created.isoformat(), "ts": event.timestamp()}})
    assert token["created_at"] == created and token["age_min"] == 3
    assert token["pumpportal_event_at"] == event and token["discovered_at"] == isolated_clock[0]
    assert token["pumpportal_created_at_basis"] == "provider_created_at"


def test_future_event_clock_cannot_manufacture_creation_or_extend_residence(isolated_clock):
    token = pumpfun._parse_event({"mint": MINT, "timestamp": (isolated_clock[0] + dt.timedelta(days=1)).timestamp()})
    assert token["pumpportal_event_at"] is None and token["created_at"] is None
    assert pumpfun._enqueue_event(token)
    isolated_clock[0] += dt.timedelta(minutes=61)
    assert pumpfun._prune_queue_state() == (1, 0)
    assert not pumpfun._pending and not pumpfun._seen and not pumpfun._buffer


@pytest.mark.parametrize("field", ["created_at", "discovered_at"])
@pytest.mark.parametrize("offset", [-61, 1])
def test_expired_or_future_incoming_clock_does_not_consume_capacity(field, offset, isolated_clock):
    assert pumpfun._enqueue_event({"address": "fresh", "created_at": isolated_clock[0]})
    assert not pumpfun._enqueue_event({"address": "invalid", field: isolated_clock[0] + dt.timedelta(minutes=offset)})
    assert [row["address"] for row in pumpfun._drain_events(2)] == ["fresh"]


def test_raw_discovery_can_get_a_receipt_without_fabricated_creation(isolated_clock):
    incoming = {"address": "A"}
    assert pumpfun._enqueue_event(incoming)
    isolated_clock[0] += dt.timedelta(minutes=1)
    result = pumpfun._drain_events(1)[0]
    assert incoming == {"address": "A"}
    assert result["discovered_at"] == isolated_clock[0] - dt.timedelta(minutes=1)
    assert "created_at" not in result


def test_unknown_birth_does_not_block_a_fresh_provider_birth_in_common_entry(isolated_clock):
    from runtime.entry_observation import prepare_entry_candidate
    from utils.market_observation import stamp_market_observation
    queued = pumpfun._parse_event({"mint": MINT})
    birth = isolated_clock[0] - dt.timedelta(minutes=4)
    market = {"address": MINT, "price_usd": 1., "created_at": birth}
    market = stamp_market_observation(market, source="jupiter")
    result = prepare_entry_candidate(queued, market)
    assert result["created_at"] == birth
    assert result["discovered_at"] == isolated_clock[0]
    assert result["pumpportal_created_at_basis"] == "unknown"


def test_case_sensitive_discovery_identities_are_not_merged(isolated_clock):
    assert pumpfun._enqueue_event({"address": "Aa", "created_at": isolated_clock[0]})
    assert pumpfun._enqueue_event({"address": "aa", "created_at": isolated_clock[0]})
    assert [row["address"] for row in pumpfun._drain_events(2)] == ["Aa", "aa"]


def test_pending_and_delivered_ownership_stay_bounded_and_disjoint(isolated_clock):
    for index in range(100):
        assert pumpfun._enqueue_event({"address": str(index), "created_at": isolated_clock[0]})
    assert len(pumpfun._pending) == len(pumpfun._buffer) == 2 and not pumpfun._seen
    assert len(pumpfun._drain_events(2)) == 2
    assert len(pumpfun._seen) == 2 and not pumpfun._pending
    isolated_clock[0] += dt.timedelta(minutes=11)
    pumpfun._prune_queue_state()
    assert not pumpfun._pending and not pumpfun._seen


@pytest.mark.asyncio
async def test_actual_ws_consumer_and_public_drain_preserve_original_discovery(isolated_clock, monkeypatch):
    messages = [SimpleNamespace(type=pumpfun.aiohttp.WSMsgType.TEXT, data=value) for value in (
        "bad json", json.dumps({"message": "subscription acknowledged"}),
        json.dumps({"mint": MINT, "timestamp": isolated_clock[0].timestamp()}))]
    subscriptions, closed = [], []
    class WebSocket:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): closed.append("ws")
        async def send_json(self, payload): subscriptions.append(payload)
        async def receive(self):
            if messages: return messages.pop(0)
            raise asyncio.CancelledError
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): closed.append("session")
        def ws_connect(self, *args, **kwargs): return WebSocket()
    monkeypatch.setattr(pumpfun.aiohttp, "ClientSession", Session)
    with pytest.raises(asyncio.CancelledError):
        await pumpfun._ws_consumer()
    assert subscriptions == [{"method": "subscribeNewToken"}] and closed == ["ws", "session"]
    async def already_started(): return None
    monkeypatch.setattr(pumpfun, "_ensure_started", already_started)
    result = await pumpfun.get_latest_pumpfun()
    assert len(result) == 1 and result[0]["address"] == MINT
    assert result[0]["created_at"] is None and result[0]["age_min"] is None
    assert result[0]["pumpportal_event_at"] == result[0]["discovered_at"] == isolated_clock[0]
    assert not pumpfun._pending and pumpfun._seen == {MINT: isolated_clock[0]}
