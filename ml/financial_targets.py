"""Verify new financial outcome contracts without rewriting legacy diagnostics."""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from runtime.trade_learning import VERSION, validate_source
from ml.data_contract import normalize_sample_type


def checked_net_return(row):
    try:
        if row.get("outcome_return_basis") != VERSION or normalize_sample_type(row.get("sample_type")) != "trade_close":
            return None
        source = json.loads(row["outcome_execution_proof"])
        net = validate_source(source)
        target = float(row["target_total_pnl_pct"])
        if (not math.isfinite(target) or source["trade_id"] != row.get("outcome_trade_id")
                or source["payload_sha256"] != row.get("outcome_source_sha256")
                or source["trade"]["token_address"] != row.get("address")
                or pd.to_datetime(source["entry_features"]["vector"]["timestamp"], utc=True) != pd.to_datetime(row.get("timestamp"), utc=True)
                or pd.to_datetime(source["trade"]["closed_at"], utc=True) != pd.to_datetime(row.get("outcome_closed_at"), utc=True)
                or not math.isclose(net, target, rel_tol=1e-6, abs_tol=1e-6)):
            return None
        return net
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return None


def declared_financial_rows(frame):
    """A broken declared proof cannot fall through to a gross legacy column."""
    mask = pd.Series(False, index=frame.index)
    for column in ("outcome_return_basis", "outcome_execution_proof", "outcome_trade_id", "outcome_source_sha256"):
        if column in frame:
            mask |= frame[column].notna() & frame[column].astype("string").str.strip().ne("").fillna(False)
    return mask


def apply_checked_net_returns(frame):
    out = frame.copy()
    declared = declared_financial_rows(out)
    if not declared.any(): return out
    for column in ("realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct", "label"):
        if column not in out: out[column] = np.nan
        else: out[column] = pd.to_numeric(out[column], errors="coerce").astype("float64")
    for position in np.flatnonzero(declared.to_numpy()):
        row = out.iloc[position].copy()
        net = checked_net_return(row)
        for column in ("realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct"):
            out.iat[position, out.columns.get_loc(column)] = np.nan if net is None else net
        threshold = json.loads(row["outcome_execution_proof"])["entry_features"]["positive_pnl_ratio"] if net is not None else None
        out.iat[position, out.columns.get_loc("label")] = np.nan if net is None else int(net / 100 >= threshold)
        if net is None:
            for column in ("max_pnl_seen", "max_pnl_pct_seen", "peak_pnl_pct", "max_pnl_pct", "outcome_closed_at"):
                if column in out: out.iat[position, out.columns.get_loc(column)] = None
    return out
