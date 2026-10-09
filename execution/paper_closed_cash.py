"""Independently conserve original closed PAPER quote cash at each fill clock.

No provider calls, current FX, historical repairs, live execution claims or
return ceiling. Scalar self-consistency and a recomputed hash are not proof.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Mapping

from execution import paper_cash_mark as cash, paper_execution_fx as fx
from execution.quote_observation import MAX_RECEIPT_AGE_SECONDS
from execution.quote_receipt import valid_summary
from utils.raw_units import sol_to_lamports

VERSION = "paper_original_closed_cash_v1"


def _same(actual, expected, *, sol=False):
    if (isinstance(actual, bool) or not isinstance(actual, (int, float))
            or not math.isfinite(actual) or not math.isfinite(expected)
            or not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12 if sol else 1e-9)):
        raise ValueError("Closed PAPER cash differs from its original receipts")


def _quote(route, *, source, target, quantity, filled, earliest):
    if not valid_summary(route, input_mint=source, output_mint=target, amount=quantity, not_after=filled):
        raise ValueError("Missing original closed PAPER quote")
    received = cash._time(route["observation_receipt"]["other"]["received_at_utc"])
    if (received < earliest or not 0 <= (filled - received).total_seconds() <= MAX_RECEIPT_AGE_SECONDS):
        raise ValueError("Original closed PAPER quote clock conflicts")


def reconstruct(trade: Mapping) -> dict:
    """Derive cash independently; optional primary fields never become inputs."""
    from runtime.paper_archive import entry_identity
    from utils.solana_addr import is_valid_base58_32
    from execution.paper_execution_cost import validate_entry as validate_cost

    if not isinstance(trade, Mapping):
        raise ValueError("Unknown closed PAPER state")
    validate_cost(trade)
    opened, closed = cash._time(trade.get("opened_at")), cash._time(trade.get("closed_at"))
    identity, token = entry_identity(trade), trade.get("token_address")
    if (trade.get("dry_run") is not True or trade.get("closed") is not True
            or cash._quantity(trade.get("qty_lamports")) != 0 or closed < opened
            or identity is None or trade.get("buy_signature") != "SIM-" + identity
            or not is_valid_base58_32(token) or trade.get("quantity_basis") != "quoted_raw_spl_units"
            or trade.get("price_source_close") != "jupiter_reverse_quote"):
        raise ValueError("Unknown original closed PAPER identity")
    amount = cash._number(trade.get("amount_sol"), positive=True)
    notional = cash._number(trade.get("entry_notional_usd"), positive=True)
    buy_price = cash._number(trade.get("buy_price_usd"), positive=True)
    quantity = cash._quantity(trade.get("entry_qty"), positive=True)
    if not fx.validate_entry(trade, amount_sol=amount, not_before=opened, not_after=opened):
        raise ValueError("Missing original closed PAPER entry FX")
    model = trade.get("execution_cost_model")
    if (not isinstance(model, Mapping) or model.get("version") != "estimated-v1"
            or model.get("observed_execution") is not False):
        raise ValueError("Unknown original PAPER cost model")
    slip, fee = cash._number(model.get("slippage_bps")), cash._number(model.get("fee_sol_per_fill"))
    if slip >= 10000:
        raise ValueError("Invalid original PAPER slippage")
    entry_quote = trade.get("entry_route_quote")
    # Entry quote can precede the buy; freshness is checked at the original buy.
    _quote(entry_quote, source=cash.SOL_MINT, target=token, quantity=sol_to_lamports(amount),
           filled=opened, earliest=opened - dt.timedelta(seconds=MAX_RECEIPT_AGE_SECONDS))
    if quantity != int(entry_quote["out_amount"] / (1 + slip / 10000)):
        raise ValueError("Closed PAPER entry raw quantity conflicts")
    events, fills = trade.get("exit_fill_events"), trade.get("execution_fill_count")
    if type(fills) is not int or fills < 2 or not isinstance(events, list) or len(events) != fills - 1:
        raise ValueError("Incomplete original closed PAPER fill population")
    remaining, previous, seen = quantity, opened, set()
    sol, usd, fees_usd = 0., 0., fee * fx._rate(trade["entry_fx_observation"], opened)
    prefix_sol, prefix_usd, prefix_qty = 0., 0., 0
    partials = []
    for index, event in enumerate(events):
        if not isinstance(event, Mapping) or not isinstance(event.get("response"), Mapping):
            raise ValueError("Malformed original closed PAPER fill")
        response, intent = event["response"], event.get("intent_id")
        at = cash._time(response.get("filled_at"))
        sold = cash._quantity(response.get("qty_sold"), positive=True)
        left = cash._quantity(response.get("qty_left"))
        partial = index < len(events) - 1
        if (not isinstance(intent, str) or re.fullmatch(r"[0-9a-f]{32}", intent) is None
                or intent in seen or response.get("exit_intent_id") != intent
                or response.get("signature") != "SIM-EXIT-" + intent or response.get("ok") is not True
                or response.get("venue") != "paper" or response.get("price_source_close") != "jupiter_reverse_quote"
                or cash._quantity(event.get("qty_before"), positive=True) != remaining
                or sold > remaining or left != remaining - sold or response.get("partial") is not partial
                or partial != (left > 0) or not previous <= at <= closed or not fx.validate_exit(response)):
            raise ValueError("Original closed PAPER fill lineage conflicts")
        route = response.get("exit_route_quote")
        _quote(route, source=token, target=cash.SOL_MINT, quantity=sold, filled=at, earliest=previous)
        if route["max_impact_pct"] != entry_quote["max_impact_pct"]:
            raise ValueError("Original closed PAPER impact limit differs")
        rate = cash._number(response["quote_sol_usd"], positive=True)
        fill_sol = route["out_amount"] / 1e9 * (1 - slip / 10000)
        fill_usd = fill_sol * rate
        price = buy_price * fill_usd / (notional * sold / quantity)
        _same(response.get("price_used_usd"), price)
        sol += fill_sol
        usd += fill_usd
        fees_usd += fee * rate
        if partial:
            prefix_sol, prefix_usd, prefix_qty = sol, usd, quantity - left
            partials.append((intent, at, sold, price))
        remaining, previous = left, at
        seen.add(intent)
    if remaining != 0 or previous != closed:
        raise ValueError("Closed PAPER final receipt differs from terminal clock/quantity")
    values = dict(total_proceeds_sol=sol, total_pnl_usd=usd - notional,
        estimated_fees_sol=fills * fee, estimated_fees_usd=fees_usd,
        net_total_pnl_sol=sol - amount - fills * fee,
        net_total_pnl_usd=usd - notional - fees_usd,
        net_total_pnl_pct=100 * (usd - notional - fees_usd) / notional)
    for name, value in values.items():
        _same(trade.get(name), value, sol=name.endswith("sol"))
    # Primary realized_* describe the partial prefix, NOT the final sale.
    prefix_cost = notional * prefix_qty / quantity
    optional = dict(realized_proceeds_sol=prefix_sol, realized_proceeds_usd=prefix_usd,
        realized_cost_usd=prefix_cost, realized_pnl_usd=prefix_usd - prefix_cost,
        # Existing closed-primary convention charges all trade fees here.
        net_realized_pnl_usd=prefix_usd - prefix_cost - fees_usd,
        total_pnl_pct=100 * (usd - notional) / notional,
        effective_exit_price_usd=buy_price * usd / notional,
        close_price_usd=price)
    for name, value in optional.items():
        if name in trade:
            try:
                _same(trade[name], value, sol=name.endswith("sol"))
            except ValueError as exc:
                raise ValueError("Original closed PAPER derived field differs: " + name) from exc
    if (("realized_qty" in trade and cash._quantity(trade["realized_qty"]) != prefix_qty)
            or ("partial_fill_events" in trade and (type(trade["partial_fill_events"]) is not int
                or trade["partial_fill_events"] != len(partials)))
            or ("partial_taken" in trade and trade["partial_taken"] is not bool(partials))):
        raise ValueError("Original closed PAPER partial prefix conflicts")
    if partials:
        for name, expected in (("first_partial_at", partials[0][1]), ("last_partial_at", partials[-1][1])):
            if name in trade and cash._time(trade[name]) != expected:
                raise ValueError("Original closed PAPER partial clock differs")
        if "last_partial_qty" in trade and cash._quantity(trade["last_partial_qty"]) != partials[-1][2]:
            raise ValueError("Original closed PAPER partial quantity differs")
        if "last_partial_price_usd" in trade:
            _same(trade["last_partial_price_usd"], partials[-1][3])
        if "first_partial_exit_intent_id" in trade and trade["first_partial_exit_intent_id"] != partials[0][0]:
            raise ValueError("Original closed PAPER partial intent differs")
    return values
