"""Native Dex discovery contracts; synthetic HTTP only, no bot/operator I/O."""
import asyncio
import ast
from copy import deepcopy
import datetime as dt
from types import SimpleNamespace
from pathlib import Path

import pytest

from utils import descubridor_pares as discovery


MINTS = ["So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "11111111111111111111111111111111"]
NOW = dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc)


def profile(index=0, **extra):
    return {"chainId": "solana", "tokenAddress": MINTS[index], **extra}


@pytest.fixture
def native_http(monkeypatch):
    """Replace only the session transport, keeping actual HTTP/parser paths."""
    state = SimpleNamespace(payloads={},statuses={},calls=[],closed=0,exited=0,
        hold_url=None, cleanup_hold=False)

    class Response:
        def __init__(self, url):
            self.url = url
            self.status = state.statuses.get(url, 200)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            if state.cleanup_hold:
                state.cleanup_started.set()
                await state.cleanup_release.wait()
            state.exited += 1

        def raise_for_status(self):
            if self.status >= 400:
                raise RuntimeError("synthetic HTTP fault")

        async def json(self):
            if state.hold_url == self.url:
                state.started.set()
                await state.release.wait()
            value = state.payloads.get(self.url)
            if isinstance(value, Exception):
                raise value
            return deepcopy(value)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            state.closed += 1

        def get(self, url, **kwargs):
            state.calls.append((url, kwargs))
            return Response(url)

    monkeypatch.setattr(discovery.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(discovery, "CFG", SimpleNamespace(MAX_CANDIDATES=0, DEX_HTTP_TIMEOUT=2))
    discovery._SPARSE_LOG_UNTIL.clear()
    return state


def run_payloads(state, first, second):
    state.payloads = dict(zip(discovery.URLS, [first, second]))
    return asyncio.run(discovery.fetch_candidate_pairs())


def test_documented_profile_routes_not_unscoped_pair_lookup():
    assert discovery.URLS == [f"{discovery.DEX}/token-profiles/latest/v1",
        f"{discovery.DEX}/token-profiles/recent-updates/v1"]


@pytest.mark.parametrize("field,value", [("listedAt", 1767225600000),
    ("createdAt", 1767225600), ("pairCreatedAt", 1767225600000)])
def test_legacy_clock_diagnostic_retains_aware_original_delta(monkeypatch, field, value):
    monkeypatch.setattr(discovery, "utc_now", lambda: NOW)
    assert discovery._calc_age_days({field:value}) == 281


@pytest.mark.parametrize("payload", [{}, {"ageDays":None}, {"ageDays":True},
    {"ageDays":float("nan")}, {"ageDays":float("inf")}, {"ageDays":-1},
    {"pairCreatedAt":True}, {"createdAt":0}, {"listedAt":-1},
    {"createdAt":(NOW+dt.timedelta(days=1)).timestamp()},
    {"createdAt":"broken", "ageDays":0}])
def test_legacy_clock_diagnostic_does_not_fabricate_zero(monkeypatch, payload):
    monkeypatch.setattr(discovery, "utc_now", lambda: NOW)
    assert discovery._calc_age_days(payload) is None


@pytest.mark.parametrize("metadata", [{}, {"ageDays":1000},
    {"pairCreatedAt":1, "createdAt":1, "listedAt":1},
    {"createdAt":True, "ageDays":float("nan")},
    {"age":-1, "ageDays":float("inf")}])
def test_profile_metadata_is_not_a_token_birth_filter(native_http, metadata):
    assert run_payloads(native_http, [profile(**metadata)], []) == [MINTS[0]]


def test_sources_are_combined_in_fair_original_row_order(native_http):
    first = [profile(0), profile(1)]
    second = [profile(2), profile(3)]
    original = deepcopy([first,second])
    assert run_payloads(native_http, first, second) == [MINTS[i] for i in [0,2,1,3]]
    assert [first,second] == original
    assert len(native_http.calls) == native_http.exited == 2
    assert native_http.closed == 1


def test_candidate_cap_counts_only_unique_valid_canonical_mints(native_http, monkeypatch):
    monkeypatch.setattr(discovery.CFG, "MAX_CANDIDATES", 3)
    first = [None, profile(0), profile(0), profile(1)]
    second = [profile(2,chainId="ethereum"), profile(2), profile(0), profile(3)]
    assert run_payloads(native_http, first, second) == [MINTS[i] for i in [0,2,1]]


@pytest.mark.parametrize("bad", [None, 4, "broken", [],
    {"chainId":{}}, {"chainId":"ethereum", "tokenAddress":MINTS[0]},
    {"tokenAddress":MINTS[0]}, {"chainId":"sol", "tokenAddress":MINTS[0]},
    {"chainId":"solana", "tokenAddress":True},
    {"chainId":"solana", "tokenAddress":[MINTS[0]]},
    {"chainId":"solana", "baseToken":{"address":MINTS[0]}},
    {"chainId":"solana", "address":MINTS[0]},
    {"chainId":"solana", "tokenAddress":"0x"+"A"*40}])
def test_bad_row_cannot_suppress_later_valid_profile(native_http, bad):
    assert run_payloads(native_http, [bad,profile(1)], []) == [MINTS[1]]


@pytest.mark.parametrize("first", [None, {"unknown":[]}, "broken", 5,
    RuntimeError("synthetic JSON fault"), [profile(chainId="ethereum")], []])
def test_unusable_primary_does_not_suppress_secondary(native_http, first):
    assert run_payloads(native_http, first, [profile(2)]) == [MINTS[2]]


@pytest.mark.parametrize("status", [404, 429, 500])
def test_http_failure_isolated_to_feed_and_sessions_closed(native_http, status):
    native_http.statuses[discovery.URLS[0]] = status
    assert run_payloads(native_http, [profile(0)], [profile(2)]) == [MINTS[2]]
    assert native_http.exited == 2 and native_http.closed == 1


def test_empty_valid_collections_are_not_a_provider_failure(native_http):
    assert run_payloads(native_http, [], []) == []
    assert len(native_http.calls) == 2


def test_all_unknown_envelopes_are_not_healthy_empty_discovery(native_http):
    with pytest.raises(RuntimeError, match="discovery"):
        run_payloads(native_http, None, {"unknown":[]})


@pytest.mark.parametrize("cap", [0, -1, None, "broken", True])
def test_invalid_or_unlimited_cap_does_not_drop_all_but_one(native_http, monkeypatch, cap):
    monkeypatch.setattr(discovery.CFG, "MAX_CANDIDATES", cap)
    assert run_payloads(native_http, [profile(0),profile(1)], [profile(2)]) == [MINTS[i] for i in [0,2,1]]


def test_single_documented_profile_object(native_http):
    assert run_payloads(native_http, profile(0), profile(1)) == [MINTS[0],MINTS[1]]


def test_legacy_collection_envelope_keeps_canonical_profile_contract(native_http):
    assert run_payloads(native_http, {"data":[profile(0)]}, {"tokens":[profile(1)]}) == [MINTS[0],MINTS[1]]


@pytest.mark.parametrize("value", [None, True, "broken", {}, -1, 0, float("nan"), float("inf")])
def test_invalid_http_timeout_keeps_bounded_real_transport_default(native_http, monkeypatch, value):
    monkeypatch.setattr(discovery.CFG, "DEX_HTTP_TIMEOUT", value)
    assert run_payloads(native_http, [profile(0)], []) == [MINTS[0]]
    assert [kwargs["timeout"] for _,kwargs in native_http.calls] == [20.,20.]


@pytest.mark.parametrize("value", [1767225600., "1767225600000", "2026-01-01T02:00:00+02:00",
    "2026-01-01T00:00:00Z", dt.datetime(2026,1,1), dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)])
def test_legacy_clock_diagnostic_numeric_iso_and_utc_offsets(monkeypatch, value):
    monkeypatch.setattr(discovery, "utc_now", lambda: NOW)
    assert discovery._calc_age_days({"createdAt":value}) == 281


@pytest.mark.parametrize("payload", [{"ageDays":0}, {"ageDays":"2.5"}, {"age":4}])
def test_explicit_legacy_diagnostic_age_is_typed_not_a_candidate_gate(payload):
    expected = float(payload.get("ageDays",payload.get("age")))
    assert discovery._calc_age_days(payload) == expected


def test_cancellation_drains_inflight_response_before_feed_shutdown(native_http):
    async def scenario():
        native_http.started = asyncio.Event()
        native_http.release = asyncio.Event()
        native_http.cleanup_started = asyncio.Event()
        native_http.cleanup_release = asyncio.Event()
        native_http.hold_url = discovery.URLS[0]
        native_http.cleanup_hold = True
        task = asyncio.create_task(discovery.fetch_candidate_pairs())
        await asyncio.wait_for(native_http.started.wait(),1)
        task.cancel()
        await asyncio.wait_for(native_http.cleanup_started.wait(),1)
        assert not task.done() and native_http.closed == 0
        native_http.cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert native_http.exited == native_http.closed == 1
        assert len(native_http.calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("healthy", [False, True])
def test_actual_source_owner_does_not_refresh_health_on_unknown_feeds(native_http, healthy):
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node,ast.AsyncFunctionDef)
        and node.name == "_dex_discovery_loop")
    errors = []
    admitted = []
    namespace = {"asyncio":asyncio,"DISCOVERY_INTERVAL":.01,
        "_runtime_discovery_paused":False,"_last_discovery_ok_at":"original",
        "fetch_candidate_pairs":discovery.fetch_candidate_pairs,
        "_queue_add_if_new":admitted.append,"utc_now":lambda:NOW,
        "_note_runtime_error":lambda *args:errors.append(args),
        "log":SimpleNamespace(error=lambda *args:None)}
    exec(compile(ast.Module(body=[function],type_ignores=[]),"run_bot.py","exec"),namespace)
    native_http.payloads = dict(zip(discovery.URLS,[[],[]] if healthy else [None,None]))
    async def scenario():
        ready = asyncio.Event()
        ready.set()
        owner = asyncio.create_task(namespace["_dex_discovery_loop"](ready))
        try:
            for _ in range(50):
                if native_http.closed:
                    break
                await asyncio.sleep(.01)
            assert native_http.closed == 1 and admitted == []
            if healthy:
                assert namespace["_last_discovery_ok_at"] == NOW and errors == []
            else:
                assert namespace["_last_discovery_ok_at"] == "original"
                assert len(errors) == 1 and errors[0][0] == "fetch_candidate_pairs"
        finally:
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
    asyncio.run(scenario())
