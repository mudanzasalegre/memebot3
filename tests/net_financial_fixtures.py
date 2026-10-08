"""Synthetic receipts only, rechecked by production validation; no real trades."""
from __future__ import annotations

import json
import math
from copy import deepcopy

import pandas as pd

from runtime import trade_learning as learning


def net_frame(frame):
    rows = []
    for index, original in enumerate(frame.to_dict(orient="records")):
        row = dict(original)
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
        from features.auxiliary_semantics import checked_row_receipt
        original_entry = checked_row_receipt(row)
        entry = (deepcopy(original_entry) if original_entry is not None else
                 learning.freeze_entry_features(row, address=row["address"], captured_at=opened,
                                                positive_pnl_ratio=0.))
        pnl, fees = net / 10, .005
        gross = pnl + fees
        peak = row.get("max_pnl_pct_seen")
        trade = {
            "entry_intent_id": identity, "buy_signature": "SIM-" + identity,
            "token_address": row["address"], "run_id": "SYNTHETIC-NET-TRAINING",
            "dry_run": True, "closed": True, "opened_at": opened.isoformat(), "closed_at": closed.isoformat(),
            "qty_lamports": 0, "entry_qty": 1000, "buy_price_usd": 1., "amount_sol": .1,
            "entry_notional_usd": 10., "quantity_basis": "quoted_raw_spl_units",
            "execution_cost_model": {"version": "estimated-v1", "observed_execution": False,
                                     "slippage_bps": 0., "fee_sol_per_fill": .000025},
            "entry_route_quote": {"in_amount": 100000000, "out_amount": 1000, "route_count": 1,
                                  "impact_bps": 1, "max_impact_pct": 3.},
            "price_source_close": "jupiter_reverse_quote", "net_total_pnl_usd": pnl,
            "net_total_pnl_pct": net, "net_total_pnl_sol": pnl / 100,
            "total_pnl_usd": gross, "estimated_fees_usd": fees, "estimated_fees_sol": .00005,
            "execution_fill_count": 2, "total_proceeds_sol": .1 + gross / 100,
            "max_pnl_pct_seen": None if pd.isna(peak) else float(peak),
            "exit_fill_events": [{"intent_id": exit_id, "qty_before": 1000, "response": {
                "ok": True, "signature": "SIM-EXIT-" + exit_id, "exit_intent_id": exit_id,
                "venue": "paper", "price_source_close": "jupiter_reverse_quote",
                "price_used_usd": 1 + gross / 10, "qty_sold": 1000, "qty_left": 0,
                "partial": False, "filled_at": closed.isoformat()}}],
        }
        source = {"version": learning.VERSION, "trade_id": identity, "entry_features": entry,
                  "buy_proof": {"intent_id": identity, "address": row["address"], "run_id": trade["run_id"],
                                "amount_sol": .1, "fill": {"qty_lamports": 1000, "buy_price_usd": 1.,
                                "entry_notional_usd": 10., "signature": "SIM-" + identity}}, "trade": trade}
        source["payload_sha256"] = learning._hash(source)
        assert learning.validate_source(source) == net
        row.update(sample_type="trade_close", outcome_return_basis=learning.VERSION, outcome_trade_id=identity,
                   outcome_source_sha256=source["payload_sha256"], outcome_execution_proof=json.dumps(source),
                   outcome_closed_at=closed, outcome_gross_pnl_pct=gross * 10,
                   label=int(net > 0))
        rows.append(row)
    return pd.DataFrame(rows)
