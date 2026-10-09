"""Synthetic receipts only, rechecked by production validation; no real trades."""
from __future__ import annotations

import json
import math
from copy import deepcopy
from hashlib import sha256

import base58

import pandas as pd

from runtime import trade_learning as learning


def mint_for(label):
    from utils.solana_addr import is_valid_base58_32
    return label if is_valid_base58_32(label) else base58.b58encode(sha256(str(label).encode()).digest()).decode()


def net_frame(frame, *, fee_sol_per_fill=None):
    rows = []
    for index, original in enumerate(frame.to_dict(orient="records")):
        row = dict(original)
        from features.auxiliary_semantics import checked_row_receipt
        original_entry = checked_row_receipt(row)
        row["address"] = mint_for(row["address"])
        if "mint" in row: row["mint"] = row["address"]
        if original_entry is not None:
            assert original_entry["vector"]["address"] == row["address"], "Use a valid original synthetic mint before freezing feature receipts"
        try:
            net = float(row["target_total_pnl_pct"])
        except (TypeError, ValueError):
            net = float("nan")
        if not math.isfinite(net):
            rows.append(row)
            continue
        opened = pd.to_datetime(row["timestamp"], utc=True)
        closed = pd.to_datetime(row.get("ts", opened + pd.Timedelta(minutes=2)), utc=True)
        identity, exit_id = f"{index + 1:032x}", f"{index + 100000:032x}"
        entry = (deepcopy(original_entry) if original_entry is not None else
                 learning.freeze_entry_features(row, address=row["address"], captured_at=opened,
                                                positive_pnl_ratio=0.))
        fee = max(.000025, -(10. + net / 10.) / 200. + .000025) if fee_sol_per_fill is None else fee_sol_per_fill
        assert type(fee) in (int, float) and math.isfinite(fee) and fee >= 0
        fees = 200. * fee
        output = round((10. + net / 10. + fees) / 100. * 1e9)
        assert output > 0
        cash = output / 1e9 * 100.
        gross, pnl = cash - 10., cash - 10. - fees
        requested_net, net = net, pnl * 10.
        assert math.isclose(net, requested_net, rel_tol=1e-9, abs_tol=1e-6)
        from execution.quote_receipt import capture_summary
        from quote_fixtures import SOL, v1_quote
        from execution.paper_execution_fx import VERSION as FX_VERSION, ENTRY_FIELDS as FX_FIELDS
        from utils.sol_price import SolUsdObservation
        entry_q = v1_quote(SOL, row["address"], 100000000, 1000, now=opened)
        close_q = v1_quote(row["address"], SOL, 1000, output, now=closed)
        entry_route = capture_summary(entry_q, input_mint=SOL, output_mint=row["address"],
            amount=100000000, slippage=entry_q.other["slippageBps"], limit=3., now=opened)
        exit_route = capture_summary(close_q, input_mint=row["address"], output_mint=SOL,
            amount=1000, slippage=close_q.other["slippageBps"], limit=3., now=closed)
        def fx(at):
            return SolUsdObservation("OK", 100., at.timestamp(), at.timestamp()).to_dict()
        peak = row.get("max_pnl_pct_seen")
        trade = {
            "entry_intent_id": identity, "buy_signature": "SIM-" + identity,
            "token_address": row["address"], "run_id": "SYNTHETIC-NET-TRAINING",
            "dry_run": True, "closed": True, "opened_at": opened.isoformat(), "closed_at": closed.isoformat(),
            "qty_lamports": 0, "entry_qty": 1000, "buy_price_usd": 1., "amount_sol": .1,
            "entry_notional_usd": 10., "quantity_basis": "quoted_raw_spl_units",
            "execution_cost_model": {"version": "estimated-v1", "observed_execution": False,
                                     "slippage_bps": 0., "fee_sol_per_fill": fee},
            "entry_route_quote": entry_route,
            "paper_execution_fx_version": FX_VERSION, "entry_valued_at": opened.isoformat(),
            "entry_fx_observation": fx(opened),
            "price_source_close": "jupiter_reverse_quote", "net_total_pnl_usd": pnl,
            "net_total_pnl_pct": net, "net_total_pnl_sol": pnl / 100,
            "total_pnl_usd": gross, "estimated_fees_usd": fees, "estimated_fees_sol": 2. * fee,
            "execution_fill_count": 2, "total_proceeds_sol": output / 1e9,
            "max_pnl_pct_seen": None if pd.isna(peak) else float(peak),
            "exit_fill_events": [{"intent_id": exit_id, "qty_before": 1000, "response": {
                "ok": True, "signature": "SIM-EXIT-" + exit_id, "exit_intent_id": exit_id,
                "venue": "paper", "price_source_close": "jupiter_reverse_quote",
                "price_used_usd": cash / 10., "qty_sold": 1000, "qty_left": 0,
                "paper_execution_fx_version": FX_VERSION, "fill_fx_observation": fx(closed),
                "quote_sol_usd": 100., "exit_route_quote": exit_route,
                "partial": False, "filled_at": closed.isoformat()}}],
        }
        from execution.paper_execution_cost import capture, ENTRY_FIELDS
        trade.update(capture(trade["execution_cost_model"], at=opened))
        source = {"version": learning.VERSION, "trade_id": identity, "entry_features": entry,
                  "buy_proof": {"intent_id": identity, "address": row["address"], "run_id": trade["run_id"],
                                "amount_sol": .1, "fill": {"qty_lamports": 1000, "buy_price_usd": 1.,
                                "entry_notional_usd": 10., "signature": "SIM-" + identity,
                                **{name: deepcopy(trade[name]) for name in (*ENTRY_FIELDS, *FX_FIELDS)}}}, "trade": trade}
        source["payload_sha256"] = learning._hash(source)
        assert math.isclose(learning.validate_source(source), net, rel_tol=1e-9, abs_tol=1e-7)
        row.update(sample_type="trade_close", outcome_return_basis=learning.VERSION, outcome_trade_id=identity,
                   outcome_source_sha256=source["payload_sha256"], outcome_execution_proof=json.dumps(source),
                   outcome_closed_at=closed, outcome_gross_pnl_pct=gross * 10, target_total_pnl_pct=net,
                   label=int(net > 0))
        rows.append(row)
    return pd.DataFrame(rows)
