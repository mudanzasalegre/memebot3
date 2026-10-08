"""Synthetic protocol responses only: no wallet, order, market-profit evidence."""
from __future__ import annotations

import asyncio
import copy
from decimal import Decimal

import aiohttp
import pytest

from fetcher import jupiter_router as router

SOL = router.SOL_MINT
TOKEN = "TokenSyntheticMint"
MID = "IntermediateSyntheticMint"
AMOUNT = 100_000_000


def hop(source=SOL, target=TOKEN, amount=AMOUNT, output=2_000_000, percent=100):
    return {"swapInfo": {"ammKey": "SyntheticAmm", "inputMint": source,
                         "outputMint": target, "inAmount": str(amount), "outAmount": str(output)},
            "percent": percent}


def payload(amount=AMOUNT, output=2_000_000):
    return {"inputMint": SOL, "outputMint": TOKEN, "inAmount": str(amount),
            "outAmount": str(output), "otherAmountThreshold": str(max(1, output * 99 // 100)),
            "swapMode": "ExactIn", "slippageBps": 100, "priceImpactPct": "0.0001",
            "routePlan": [hop(amount=amount, output=output)], "contextSlot": 324307186}


def checked(body, amount=AMOUNT, direct=False):
    return router._checked_quote(body, input_mint=SOL, output_mint=TOKEN,
                                 amount=amount, slippage=100, direct=direct)


def test_direct_quote_is_detached_and_not_a_market_or_fill_attestation():
    body = payload()
    q = checked(body, direct=True)
    assert q.ok and q.in_amount == AMOUNT and q.out_amount == 2_000_000
    assert q.price_impact_bps == pytest.approx(1)
    assert q.other["quote_contract_version"] == 1
    assert q.other["requested_in_amount"] == AMOUNT
    assert q.other["received_at_utc"].endswith("+00:00")
    assert q.other["market_asof_verified"] is False
    assert q.other["fill_verified"] is False
    body["routePlan"][0]["swapInfo"]["outAmount"] = "3"
    assert q.raw["routePlan"][0]["swapInfo"]["outAmount"] == "2000000"


def test_multihop_does_not_sum_percentages_or_replace_whole_route_output():
    body = payload()
    body["routePlan"] = [hop(SOL, MID, AMOUNT, 300), hop(MID, TOKEN, 300, 2_000_000)]
    q = checked(body)
    assert q.ok and q.out_amount == 2_000_000 and q.other["routePlan_len"] == 2
    assert not checked(body, direct=True).ok


def test_split_and_multihop_paths_with_bps_and_optional_percent_are_supported():
    body = payload()
    body["routePlan"] = [hop(SOL, TOKEN, AMOUNT // 2, 1_000_000, 50),
                         hop(SOL, MID, AMOUNT // 2, 200, 50),
                         hop(MID, TOKEN, 200, 1_000_000)]
    body["routePlan"][0].update(percent=None, bps=5000)
    assert checked(body).ok
    body["routePlan"] = [hop(amount=AMOUNT // 2, output=1_000_000, percent=50)] * 2
    assert checked(body, direct=True).ok


@pytest.mark.parametrize("amount", [2**53 + 1, 2**63 + 123, 2**64 - 1])
def test_large_raw_amounts_do_not_pass_through_float(amount):
    body = payload(amount=amount, output=amount - 1)
    q = checked(body, amount=amount)
    assert q.ok and q.in_amount == amount and q.out_amount == amount - 1


@pytest.mark.parametrize("field,value", [
    ("inputMint", TOKEN), ("outputMint", MID), ("inAmount", "100000001"),
    ("outAmount", "0"), ("swapMode", "ExactOut"), ("swapMode", None),
    ("slippageBps", 101), ("slippageBps", True), ("slippageBps", "100"),
    ("otherAmountThreshold", "0"), ("otherAmountThreshold", "2000001"),
    ("priceImpactPct", None), ("priceImpactPct", "nan"), ("priceImpactPct", float("inf")),
    ("priceImpactPct", "-0.001"), ("priceImpactPct", "3.2"), ("priceImpactPct", True),
    ("priceImpactPct", " 0.01"), ("contextSlot", True), ("contextSlot", -1),
    ("contextSlot", 2**64), ("contextSlot", 1.5), ("routePlan", []), ("routePlan", {}),
    ("routePlan", [None]), ("routePlan", [{"swapInfo": {}}]),
    ("error", "NO_ROUTES_FOUND"), ("errorCode", "TOKEN_NOT_TRADABLE"),
])
def test_bad_contract_has_no_usable_partial_evidence(field, value):
    body = payload()
    body[field] = value
    q = checked(body)
    assert not q.ok and q.in_amount is None and q.out_amount is None and q.price_impact_bps is None
    assert q.other["quote_contract_error"]


@pytest.mark.parametrize("value", [True, False, 100_000_000.0, "100000000.0", "1e8",
                                   "-1", " 100000000", "١٠٠٠٠٠٠٠٠", 2**64, str(2**64), None])
@pytest.mark.parametrize("field", ["inAmount", "outAmount", "otherAmountThreshold"])
def test_raw_response_units_are_strict(field, value):
    body = payload()
    body[field] = value
    assert not checked(body).ok


@pytest.mark.parametrize("field", ["inAmount", "outAmount", "priceImpactPct"])
def test_first_hop_cannot_fill_missing_whole_route_fields(field):
    body = payload()
    body["routePlan"][0]["swapInfo"][field] = body.pop(field)
    assert not checked(body).ok


@pytest.mark.parametrize("field,value", [("ammKey", ""), ("inputMint", None),
                                        ("inAmount", "1.0"), ("outAmount", True)])
def test_bad_hop_is_rejected(field, value):
    body = payload()
    body["routePlan"][0]["swapInfo"][field] = value
    assert not checked(body).ok


@pytest.mark.parametrize("key,value", [("percent", 101), ("percent", 99.5),
                                      ("percent", True), ("bps", 10001), ("bps", "5000")])
def test_bad_weight_is_rejected_without_false_total_weight_gate(key, value):
    body = payload()
    body["routePlan"][0][key] = value
    assert not checked(body).ok


def test_disconnected_and_resource_unbounded_plans_are_not_routes():
    body = payload()
    body["routePlan"].append(hop("Unrelated", "Detached"))
    assert not checked(body).ok
    body["routePlan"] = [hop(SOL, MID)]
    assert not checked(body).ok
    body["routePlan"] = [hop()] * (router._MAX_QUOTE_ROUTE_STEPS + 1)
    assert not checked(body).ok


@pytest.mark.parametrize("body", [None, [], "quote", 123, True])
def test_non_object_response_is_a_false_quote_not_an_exception(body):
    assert not checked(body).ok


@pytest.mark.parametrize("value,expected", [("0", 0), ("0.01", 100), (0.5, 5000), ("1", 10000)])
def test_fraction_units_are_not_guessed(value, expected):
    body = payload()
    body["priceImpactPct"] = value
    assert checked(body).price_impact_bps == expected


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        if isinstance(self.body, BaseException):
            raise self.body
        return copy.deepcopy(self.body)

    async def text(self):
        return "synthetic HTTP error"


@pytest.fixture
def network(monkeypatch):
    calls, replies = [], []

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, params):
            calls.append((url, dict(params)))
            assert replies, "Unplanned HTTP request"
            return replies.pop(0)

    monkeypatch.setattr(router.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(router, "JUP_QUOTE_URL", router._LITE_QUOTE_URL)
    monkeypatch.setattr(router, "JUP_API_KEY", "")
    monkeypatch.setattr(router, "DEFAULT_SLIPPAGE_BPS", 100)
    return calls, replies


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [0.1, Decimal("0.1000000009")])
async def test_actual_get_quote_sends_exact_01_sol_and_explicit_mode(network, amount):
    calls, replies = network
    replies.append(Response(payload()))
    q = await router.get_quote(input_mint=SOL, output_mint=TOKEN, amount_sol=amount)
    assert q.ok and len(calls) == 1
    assert calls[0][1]["amount"] == "100000000"
    assert calls[0][1]["swapMode"] == "ExactIn"


@pytest.mark.asyncio
async def test_raw_token_alias_and_large_units_preserved_over_http(network):
    calls, replies = network
    amount = 2**53 + 1
    replies.append(Response(payload(amount=amount)))
    q = await router.get_quote(input_mint=SOL, output_mint=TOKEN, amount_tokens=amount)
    assert q.ok and calls[0][1]["amount"] == str(amount)


@pytest.mark.asyncio
async def test_equal_legacy_raw_aliases_are_not_a_conflict(network):
    _, replies = network
    replies.append(Response(payload()))
    assert (await router.get_quote(input_mint=SOL, output_mint=TOKEN,
                                   amount_lamports=AMOUNT, amount_tokens=AMOUNT)).ok


@pytest.mark.asyncio
@pytest.mark.parametrize("mint", [None, True, "", " padded "])
async def test_invalid_mint_request_does_not_fetch(network, mint):
    calls, _ = network
    assert not (await router.get_quote(input_mint=mint, output_mint=TOKEN, amount_sol=0.1)).ok
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"amount_lamports": True}, {"amount_lamports": 1.5}, {"amount_lamports": "100000000"},
    {"amount_lamports": 0}, {"amount_lamports": -1}, {"amount_lamports": 2**64},
    {"amount_sol": True}, {"amount_sol": float("inf")}, {"amount_sol": float("nan")},
    {"amount_sol": -0.1}, {"amount_sol": 1e-10}, {"amount_sol": "0.1"},
    {"amount_lamports": AMOUNT, "amount_tokens": AMOUNT + 1},
    {"amount_sol": 0.1, "amount_tokens": AMOUNT},
    {"amount_lamports": AMOUNT, "slippage_bps": True},
    {"amount_lamports": AMOUNT, "slippage_bps": 1.5},
    {"amount_lamports": AMOUNT, "slippage_bps": "100"},
    {"amount_lamports": AMOUNT, "slippage_bps": -1},
    {"amount_lamports": AMOUNT, "slippage_bps": 65536},
    {"amount_lamports": AMOUNT, "only_direct_routes": "false"},
])
async def test_invalid_requests_never_fetch(network, kwargs):
    calls, _ = network
    q = await router.get_quote(input_mint=SOL, output_mint=TOKEN, **kwargs)
    assert not q.ok and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [(None, 200), ([], 200), (ValueError("bad JSON"), 200),
                                         (aiohttp.ClientError("offline"), 200), ({}, 429)])
async def test_http_failure_or_bad_json_returns_unknown(network, body, status):
    _, replies = network
    replies.append(Response(body, status))
    assert not (await router.get_quote(input_mint=SOL, output_mint=TOKEN, amount_sol=0.1)).ok


@pytest.mark.asyncio
async def test_http_cancellation_propagates_without_fallback(network):
    calls, replies = network
    replies.append(Response(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await router.get_quote(input_mint=SOL, output_mint=TOKEN, amount_sol=0.1)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("primary", ["api", "lite"])
async def test_each_fallback_rechecks_full_response_and_lite_host_is_not_api(network, monkeypatch, primary):
    calls, replies = network
    monkeypatch.setattr(router, "JUP_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(router, "JUP_QUOTE_URL", router._API_QUOTE_URL if primary == "api" else router._LITE_QUOTE_URL)
    wrong = payload()
    wrong["outputMint"] = MID
    replies.extend([Response(wrong), Response(payload())])
    q = await router.get_quote(input_mint=SOL, output_mint=TOKEN, amount_sol=0.1)
    assert q.ok and q.raw["outputMint"] == TOKEN
    assert len(calls) == 2 and calls[0][0] != calls[1][0]
    assert calls[0][1] == calls[1][1]


@pytest.mark.asyncio
async def test_malformed_custom_url_does_not_crash_fallback(network, monkeypatch):
    calls, replies = network
    monkeypatch.setattr(router, "JUP_QUOTE_URL", "http://[malformed")
    monkeypatch.setattr(router, "_preferred_quote_url", lambda: router._LITE_QUOTE_URL)
    replies.extend([Response({}, status=400), Response(payload())])
    assert (await router.get_quote(input_mint=SOL, output_mint=TOKEN, amount_sol=0.1)).ok
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_mint", [False, True])
async def test_actual_paper_route_probe_consumes_checked_http_contract(network, monkeypatch, wrong_mint):
    from trader import papertrading as paper
    calls, replies = network
    body = payload()
    if wrong_mint:
        body["outputMint"] = MID
    replies.append(Response(body))
    monkeypatch.setattr(paper, "SOL_MINT", SOL)
    proof = {}
    ok, reason = await paper._has_jupiter_route(TOKEN, amount_sol=0.1, proof=proof)
    assert ok is (not wrong_mint)
    assert reason == ("NO_QUOTE" if wrong_mint else "QUOTE_OK")
    assert len(calls) == 1
    if ok:
        assert proof["in_amount"] == AMOUNT and proof["route_count"] == 1
    else:
        assert not proof


@pytest.mark.asyncio
@pytest.mark.parametrize("multiple", [6, 11, 51, 101])
async def test_actual_01_paper_entry_wrong_exit_and_extreme_partials_use_http_contract(
        network, monkeypatch, tmp_path, multiple):
    """Synthetic 6x/11x/51x/101x routes test accounting, not achievable gains."""
    from dataclasses import replace
    from unittest.mock import AsyncMock
    from trader import papertrading as paper
    from analytics.forward_evidence import _costed_close

    mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    monkeypatch.setattr(paper, "CFG", replace(paper.CFG, PAPER_EXACT_TRADE_SIZE_ENABLED=True))
    monkeypatch.setattr(paper, "_PORTFOLIO", {})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "paper_portfolio.json")
    monkeypatch.setattr(paper, "_resolve_buy_price_usd", AsyncMock(return_value=(1.0, "jupiter")))
    monkeypatch.setattr(paper, "_resolve_entry_notional_usd", AsyncMock(return_value=10.0))
    monkeypatch.setattr(paper, "get_sol_usd", AsyncMock(return_value=100.0))
    monkeypatch.setattr(paper.jupiter_price, "get_usd_price", AsyncMock(return_value=1.0))
    monkeypatch.delenv("TRADING_HOURS", raising=False)
    monkeypatch.delenv("TRADING_HOURS_EXTRA", raising=False)
    monkeypatch.setenv("PAPER_FILL_SLIPPAGE_BPS", "100")
    monkeypatch.setenv("PAPER_FILL_FEE_SOL", "0.000025")
    calls, replies = network
    body = payload()
    body["outputMint"] = mint
    body["routePlan"][0]["swapInfo"]["outputMint"] = mint
    replies.append(Response(body))
    entry = await paper.buy(mint, 0.1, require_jupiter_for_buy=True)
    qty = entry["qty_lamports"]
    assert qty == int(2_000_000 / 1.01)
    assert paper._PORTFOLIO[mint]["amount_sol"] == 0.1

    def reverse(amount):
        output = amount * AMOUNT * multiple // 2_000_000
        quote = payload(amount=amount, output=output)
        quote.update(inputMint=mint, outputMint=SOL)
        quote["routePlan"] = [hop(mint, SOL, amount, output)]
        return quote

    bad = reverse(qty)
    bad["outputMint"] = MID
    replies.append(Response(bad))
    original = copy.deepcopy(paper._PORTFOLIO[mint])
    failed = await paper.sell(mint, qty, price_hint=1_000_000)
    assert not failed["ok"] and failed["error"] == "EXIT_QUOTE_UNAVAILABLE"
    assert paper._PORTFOLIO[mint] == original
    assert not (tmp_path / "paper_closed_trades.jsonl").exists()

    part = qty // 2
    replies.extend([Response(reverse(part)), Response(reverse(qty - part))])
    assert (await paper.sell(mint, part))["ok"]
    assert (await paper.sell(mint, qty - part))["ok"]
    closed = paper._PORTFOLIO[mint]
    assert closed["closed"] and closed["qty_lamports"] == 0
    assert closed["execution_fill_count"] == 3
    assert closed["net_total_pnl_sol"] > 0.4
    assert _costed_close(closed) is not None
    assert len(calls) == 4 and not replies
    assert calls[2][1]["inputMint"] == mint and calls[2][1]["amount"] == str(part)
    assert calls[3][1]["amount"] == str(qty - part)
