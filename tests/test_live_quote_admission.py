"""Live-code paths with synthetic HTTP and order doubles only; no real trades."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fetcher import jupiter_router as router
from trader import buyer
from runtime.buy_recovery import BuyRecoveryStore
from db.models import Position
from utils.raw_units import U64_MAX, raw_uint, sol_to_lamports
from test_jupiter_quote_contract import network, payload, Response, SOL, TOKEN, AMOUNT


@pytest.fixture
def guarded(monkeypatch):
    response = {"signature": "synthetic", "order": {"outAmount": "1000"},
                "route": {"quote": {"outAmount": "1000"}}}
    managed, legacy = AsyncMock(return_value=response), AsyncMock(return_value=response)
    monkeypatch.setattr(buyer, "_JUP_ROUTER_AVAILABLE", True)
    monkeypatch.setattr(buyer, "jupiter", router)
    monkeypatch.setattr(buyer, "_JUP_BUY_SLIPPAGE_BPS", 100)
    monkeypatch.setattr(buyer, "_IMPACT_MAX_PCT_DEFAULT", 8.)
    monkeypatch.setattr(buyer, "_REQUIRE_JUP_PRICE", True)
    monkeypatch.setattr(buyer, "is_in_trading_window", lambda: True)
    monkeypatch.setattr(buyer, "_max_positions_reached", AsyncMock(return_value=False))
    monkeypatch.setattr(buyer, "_has_enough_funds", AsyncMock(return_value=True))
    monkeypatch.setattr(buyer.jupiter_price, "get_usd_price", AsyncMock(return_value=1.))
    monkeypatch.setattr(buyer, "_resolve_buy_price_usd", AsyncMock(return_value=(1., "synthetic")))
    monkeypatch.setattr(buyer, "_resolve_entry_notional_usd", AsyncMock(return_value=10.))
    monkeypatch.setattr(router, "execute_managed_swap", managed)
    monkeypatch.setattr(buyer.gmgn, "buy", legacy)
    return managed, legacy


@pytest.mark.parametrize("value,expected", [(0.1, AMOUNT), (Decimal("0.1000000009"), AMOUNT),
    (Decimal("9007199.254740993"), 2**53 + 1), (Decimal("18446744073.709551615"), U64_MAX),
    (True, None), ("0.1", None), (float("nan"), None), (float("inf"), None),
    (-1, None), (1e-10, None), (Decimal("1e-1000000"), None), (0, None)])
def test_common_sol_unit_contract(value, expected):
    assert sol_to_lamports(value) == expected


def test_uint_and_zero_reserve_contract():
    assert raw_uint(str(2**53 + 1)) == 2**53 + 1
    assert raw_uint(True) is None and raw_uint(1.0) is None
    assert sol_to_lamports(0, allow_zero=True) == 0
    assert sol_to_lamports(1e-10, allow_zero=True) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("required,managed", [(True, True), (False, True), (True, False)])
async def test_one_actual_quote_allows_supported_route_before_one_order(network, guarded, monkeypatch, required, managed):
    calls, replies = network
    replies.append(Response(payload()))
    monkeypatch.setattr(buyer, "_REQUIRE_JUP_PRICE", required)
    monkeypatch.setattr(router, "JUP_API_KEY", "synthetic" if managed else "")
    response = await buyer.buy(TOKEN, .1)
    assert response["qty_lamports"] == 1000 and len(calls) == 1
    assert calls[0][1]["amount"] == str(AMOUNT)
    chosen = guarded[0] if managed else guarded[1]
    assert chosen.await_count == 1 and guarded[1 if managed else 0].await_count == 0
    if managed:
        assert chosen.await_args.kwargs["amount_lamports"] == AMOUNT
    else:
        chosen.assert_awaited_once_with(TOKEN, .1)


@pytest.mark.asyncio
async def test_gmgn_optional_policy_does_not_impose_a_jupiter_query(network, guarded, monkeypatch):
    calls, _ = network
    monkeypatch.setattr(buyer, "_REQUIRE_JUP_PRICE", False)
    response = await buyer.buy("display-alias", .1, token_mint=TOKEN)
    assert response["qty_lamports"] == 1000 and not calls
    guarded[1].assert_awaited_once_with(TOKEN, .1)
    buyer.jupiter_price.get_usd_price.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["wrong_mint", "wrong_amount", "no_route", "impact_missing", "impact_high", "router_error", "no_router"])
async def test_price_success_cannot_authorize_unproved_route(network, guarded, monkeypatch, fault):
    _, replies = network
    body = payload()
    if fault == "wrong_mint": body["outputMint"] = "WrongMint"
    if fault == "wrong_amount": body["inAmount"] = "1"
    if fault == "no_route": body["routePlan"] = []
    if fault == "impact_missing": body.pop("priceImpactPct")
    if fault == "impact_high": body["priceImpactPct"] = ".09"
    if fault == "router_error": body = TimeoutError("synthetic")
    if fault == "no_router": monkeypatch.setattr(buyer, "_JUP_ROUTER_AVAILABLE", False)
    else: replies.append(Response(body))
    result = await buyer.buy(TOKEN, .1)
    assert result["signature"] == ("HIGH_IMPACT" if fault == "impact_high" else "NO_JUP_ROUTE")
    assert result["qty_lamports"] == 0
    assert not guarded[0].called and not guarded[1].called


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [True, None, 0., -1., float("nan"), float("inf"), "1"])
async def test_invalid_required_price_is_not_admission(network, guarded, monkeypatch, price):
    calls, _ = network
    buyer.jupiter_price.get_usd_price.return_value = price
    assert (await buyer.buy(TOKEN, .1))["signature"] == "NO_JUP_PRICE"
    assert not calls and not guarded[0].called and not guarded[1].called


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [True, None, -1., float("nan"), float("inf"), "8"])
async def test_invalid_impact_limit_is_durable_proved_non_submission(network, guarded, monkeypatch, tmp_path, limit):
    _, replies = network
    replies.append(Response(payload()))
    monkeypatch.setattr(buyer, "_IMPACT_MAX_PCT_DEFAULT", limit)
    result = await buyer.buy(TOKEN, .1)
    assert result["signature"] == "INVALID_IMPACT_LIMIT"
    assert not guarded[0].called and not guarded[1].called
    # The pre-send guard remains an explicit no-fill, not ambiguous recovery.
    store = BuyRecoveryStore(tmp_path)
    with store.scope():
        attempt = store.begin(Position(address=TOKEN, dry_run=False, buy_amount_sol=.1,
                                       run_id="synthetic"), amount_sol=.1, paper=False)
        attempt.receive(result)
    assert attempt.row["state"] == "no_fill"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("ok", "true"), ("in_amount", True), ("out_amount", 1000.),
    ("price_impact_bps", True), ("price_impact_bps", None), ("price_impact_bps", float("nan")),
    ("price_impact_bps", -1.), ("raw", {}), ("out_amount", 999)])
async def test_consumer_rechecks_raw_response_and_public_values(guarded, monkeypatch, field, value):
    q = router._checked_quote(payload(), input_mint=SOL, output_mint=TOKEN, amount=AMOUNT, slippage=100, direct=False)
    setattr(q, field, value)
    monkeypatch.setattr(router, "get_quote", AsyncMock(return_value=q))
    assert await buyer._jupiter_precheck_quote(TOKEN, .1) == (False, None)


@pytest.mark.asyncio
async def test_route_compatibility_probe_never_reads_price(network, guarded):
    _, replies = network
    replies.append(Response(payload()))
    assert await buyer._has_jupiter_route(TOKEN, .1) == (True, "OK")
    buyer.jupiter_price.get_usd_price.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_quote_does_not_submit_or_translate_into_permission(guarded, monkeypatch):
    monkeypatch.setattr(router, "get_quote", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await buyer.buy(TOKEN, .1)
    assert not guarded[0].called and not guarded[1].called


@pytest.mark.asyncio
async def test_sub_lamport_amount_rejected_before_balance_or_provider(guarded):
    assert (await buyer.buy(TOKEN, 1e-10))["signature"] == "INVALID_AMOUNT"
    buyer._has_enough_funds.assert_not_called()
    assert not guarded[0].called and not guarded[1].called


@pytest.mark.asyncio
async def test_funds_use_same_raw_units_without_binary_float_rounding(monkeypatch):
    # float(3e-8) * 1e9 is below 30 on this Python runtime; transport wants 30.
    monkeypatch.setattr(buyer, "_WALLET_PUBKEY", "synthetic-wallet")
    monkeypatch.setattr(buyer, "_GAS_RESERVE_LAMPORTS", 0)
    balance = AsyncMock(return_value=29)
    monkeypatch.setattr(buyer, "get_balance_lamports", balance)
    assert not await buyer._has_enough_funds(3e-8)
    balance.return_value = 30
    assert await buyer._has_enough_funds(3e-8)
    monkeypatch.setattr(buyer, "_GAS_RESERVE_LAMPORTS", None)
    assert not await buyer._has_enough_funds(.1)
