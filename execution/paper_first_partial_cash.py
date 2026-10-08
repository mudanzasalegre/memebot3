"""Reconstruct the original first-partial PAPER cash, never current prices.

An intake checksum alone is not financial proof. The quoted entry, the one
original sell receipt and their own FX clocks must conserve quantity and cash.
Legacy/unproved sources stay on disk but cannot certify paired exit research.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping

from execution import paper_cash_mark as cash, paper_execution_fx as fx
from execution.quote_observation import MAX_RECEIPT_AGE_SECONDS
from execution.quote_receipt import valid_summary
from runtime.paper_archive import entry_identity

VERSION = "paper_original_first_partial_cash_v1"
CASH_FIELDS = ("realized_proceeds_sol", "realized_proceeds_usd", "estimated_fees_sol", "estimated_fees_usd")
PROOF_FIELDS = ("first_partial_exit_intent_id", "first_partial_at", "exit_fill_events")


def _same(actual, expected, *, usd=False):
    value = cash._number(actual)
    if not math.isfinite(expected) or not math.isclose(value, expected, rel_tol=1e-12,
                                                     abs_tol=1e-9 if usd else 1e-12):
        raise ValueError("First-partial cash differs from its original receipts")


def _quote_at(route, *, source, target, quantity, filled):
    if not valid_summary(route, input_mint=source, output_mint=target, amount=quantity, not_after=filled):
        raise ValueError("Missing original first-partial quote")
    received = cash._time(route["observation_receipt"]["other"]["received_at_utc"])
    if not 0 <= (filled - received).total_seconds() <= MAX_RECEIPT_AGE_SECONDS:
        raise ValueError("Original quote was stale at its fill clock")
    return received


def reconstruct(entry: Mapping, *, captured_at) -> dict:
    """Return independently derived prefix cash or reject; no I/O/backfill."""
    if not isinstance(entry, Mapping):
        raise ValueError("Unknown original first-partial state")
    opened, captured = cash._time(entry.get("opened_at")), cash._time(captured_at)
    if (entry.get("dry_run") is not True or entry.get("closed") is not False
            or entry.get("partial_taken") is not True
            or type(entry.get("partial_fill_events")) is not int or entry["partial_fill_events"] != 1
            or type(entry.get("execution_fill_count")) is not int or entry["execution_fill_count"] != 2
            or captured < opened or cash._time(entry.get("first_partial_at")) != captured
            or cash._number(entry.get("amount_sol"), positive=True) != .1
            or not fx.validate_entry(entry, amount_sol=.1, not_before=opened, not_after=opened)):
        raise ValueError("Not a proved original first-partial PAPER prefix")
    identity = entry_identity(entry)
    if identity is None:
        raise ValueError("Missing original PAPER buy identity")
    token = entry.get("token_address")
    frozen = cash.basis(entry, token=token, owner="buy:" + identity)
    _quote_at(entry["entry_route_quote"], source=cash.SOL_MINT, target=token,
              quantity=100000000, filled=opened)
    events = entry.get("exit_fill_events")
    intent = entry.get("first_partial_exit_intent_id")
    if (not isinstance(intent, str) or re.fullmatch(r"[0-9a-f]{32}", intent) is None
            or not isinstance(events, list) or len(events) != 1 or not isinstance(events[0], Mapping)):
        raise ValueError("Incomplete original first-partial fill population")
    event, response = events[0], events[0].get("response")
    if (not isinstance(response, Mapping) or event.get("intent_id") != intent
            or cash._quantity(event.get("qty_before"), positive=True) != frozen["entry_qty"]
            or response.get("ok") is not True or response.get("venue") != "paper"
            or response.get("partial") is not True or response.get("exit_intent_id") != intent
            or response.get("signature") != "SIM-EXIT-" + intent
            or cash._quantity(response.get("qty_sold"), positive=True) != frozen["realized_qty"]
            or cash._quantity(response.get("qty_left"), positive=True) != frozen["remaining_qty"]
            or cash._time(response.get("filled_at")) != captured
            or response.get("price_source_close") != "jupiter_reverse_quote"
            or not fx.validate_exit(response)):
        raise ValueError("Original first-partial fill lineage conflicts")
    route = response.get("exit_route_quote")
    received = _quote_at(route, source=token, target=cash.SOL_MINT,
                         quantity=frozen["realized_qty"], filled=captured)
    if received < opened or route["max_impact_pct"] != entry["entry_route_quote"]["max_impact_pct"]:
        raise ValueError("Original first-partial quote belongs to another entry/limit")
    model = frozen["cost_model"]
    proceeds_sol = route["out_amount"] / 1e9 * (1 - model["slippage_bps"] / 10000)
    exit_rate = cash._number(response["quote_sol_usd"], positive=True)
    entry_rate = fx._rate(entry["entry_fx_observation"], opened)
    values = dict(realized_proceeds_sol=proceeds_sol, realized_proceeds_usd=proceeds_sol * exit_rate,
        estimated_fees_sol=2 * model["fee_sol_per_fill"],
        estimated_fees_usd=model["fee_sol_per_fill"] * entry_rate + model["fee_sol_per_fill"] * exit_rate)
    for name, value in values.items():
        _same(entry.get(name), value, usd=name.endswith("usd"))
    realized_cost = frozen["entry_notional_usd"] * frozen["realized_qty"] / frozen["entry_qty"]
    price = frozen["entry_reference_price_usd"] * values["realized_proceeds_usd"] / realized_cost
    _same(response.get("price_used_usd"), price, usd=True)
    for name, value in (("realized_cost_usd", realized_cost),
            ("realized_pnl_usd", values["realized_proceeds_usd"] - realized_cost),
            ("net_realized_pnl_usd", values["realized_proceeds_usd"] - realized_cost - values["estimated_fees_usd"]),
            ("last_partial_price_usd", price)):
        if name in entry:
            # PnL is signed, unlike nonnegative cash balances.
            actual = entry[name]
            if (isinstance(actual, bool) or not isinstance(actual, (int, float))
                    or not math.isfinite(actual) or not math.isclose(actual, value, rel_tol=1e-12, abs_tol=1e-9)):
                raise ValueError("Original first-partial derived accounting conflicts")
    if (("last_partial_at" in entry and cash._time(entry["last_partial_at"]) != captured)
            or ("last_partial_qty" in entry and cash._quantity(entry["last_partial_qty"]) != frozen["realized_qty"])):
        raise ValueError("Original first-partial last-fill clock/quantity conflicts")
    return values
