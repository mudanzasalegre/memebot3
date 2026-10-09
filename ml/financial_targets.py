"""Verify new financial outcome contracts without rewriting legacy diagnostics."""
from __future__ import annotations

import json
import math
from hashlib import sha256

import numpy as np
import pandas as pd

from runtime.trade_learning import VERSION, validate_source
from execution.paper_execution_cost import VERSION as COST_VERSION
from execution.paper_closed_cash import VERSION as CASH_VERSION
from features.builder import COLUMNS
from features.auxiliary_semantics import PROOF_COLUMN
from ml.data_contract import normalize_sample_type

TRAINING_VERSION = "checked_net_training_population_v1"
TRAINING_SCOPE = "estimated_paper_execution_not_live_profit"


def financial_target(family, target):
    """Peak opportunity targets are not estimates of realised cash returns."""
    return family == "risk" or (family == "ev" and target != "ev_peak_adjusted")


def checked_financial_frame(frame):
    """One original T0 per checked trade; reject conflicting or broken copies.

    Rebuild all predictor inputs from the frozen pre-buy source. A later mutable
    dataset value, derived label or peak cannot replace those original inputs.
    Legacy/shadow rows remain available to opportunity workflows, not this one.
    """
    grouped = {}
    unchecked = 0
    for row in frame.to_dict(orient="records"):
        identity = row.get("outcome_trade_id")
        net = checked_net_return(row)
        if not isinstance(identity, str) or not identity:
            unchecked += 1
            continue
        grouped.setdefault(identity, []).append((row, net))
    selected, conflicts, duplicates, thresholds = [], [], 0, set()
    population = []
    for identity, copies in sorted(grouped.items()):
        hashes = {row.get("outcome_source_sha256") for row, net in copies if net is not None}
        if any(net is None for _, net in copies) or len(hashes) != 1:
            unchecked += len(copies)
            conflicts.append(identity)
            continue
        row, net = copies[0]
        source = json.loads(row["outcome_execution_proof"])
        restored = {**row, **source["entry_features"]["vector"]}
        restored[PROOF_COLUMN] = json.dumps(source["entry_features"], sort_keys=True, separators=(",", ":"), allow_nan=False)
        restored["timestamp"] = pd.to_datetime(restored["timestamp"], utc=True)
        restored["mint"] = restored["address"]
        restored["outcome_closed_at"] = pd.to_datetime(source["trade"]["closed_at"], utc=True)
        # Availability is the close, not a retry/export wall-clock time.
        restored["ts"] = restored["outcome_closed_at"]
        for column in ("realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct"):
            restored[column] = net
        threshold = source["entry_features"]["positive_pnl_ratio"]
        restored["label"] = int(net / 100 >= threshold)
        selected.append(restored)
        duplicates += len(copies) - 1
        thresholds.add(threshold)
        population.append([identity, source["payload_sha256"]])
    out = pd.DataFrame(selected, columns=frame.columns.union(
        pd.Index(COLUMNS + ["mint", "realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct",
                          "label", "ts", "outcome_closed_at", PROOF_COLUMN]), sort=False))
    report = {
        "version": TRAINING_VERSION, "return_basis": VERSION, "scope": TRAINING_SCOPE,
        "cost_basis_version": COST_VERSION,
        "cash_basis_version": CASH_VERSION,
        "source_rows": len(frame), "rows": len(out), "unique_trades": len(out),
        "unchecked_rows": unchecked, "duplicates_removed": duplicates,
        "conflicting_trade_ids": conflicts, "positive_pnl_ratios": sorted(thresholds),
        "population_sha256": sha256(json.dumps(population, separators=(",", ":")).encode()).hexdigest(),
        "ready": bool(len(out) and not conflicts),
    }
    return out.reset_index(drop=True), report


def supported_financial_training(metadata, *, entry=False):
    """Fail closed for legacy financial artifacts, never for peak ranking."""
    try:
        proof = metadata["financial_training"]
        fingerprint = proof["population_sha256"]
        rows = proof["rows"]
        thresholds = proof["positive_pnl_ratios"]
        return (proof.get("ready") is True and proof.get("version") == TRAINING_VERSION
                and proof.get("cost_basis_version") == COST_VERSION
                and proof.get("cash_basis_version") == CASH_VERSION
                and proof.get("return_basis") == VERSION and proof.get("scope") == TRAINING_SCOPE
                and type(rows) is int and rows > 0 and type(proof.get("unique_trades")) is int
                and proof.get("unique_trades") == rows
                and ("target_rows" not in metadata or metadata["target_rows"] == rows)
                and ("rows" not in metadata or metadata["rows"] == rows)
                and not proof.get("conflicting_trade_ids")
                and isinstance(fingerprint, str) and len(fingerprint) == 64
                and all(c in "0123456789abcdef" for c in fingerprint)
                and isinstance(thresholds, list) and bool(thresholds)
                and all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in thresholds)
                and (not entry or len(thresholds) == 1))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


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
