"""Recover missing targets, not features, from causally matched terminal rows."""
from __future__ import annotations

import bisect
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from analytics.report_utils import is_closed_trade, load_candidate_outcomes, parse_event_timestamp


def enrich_outcome_targets(frame: pd.DataFrame, root: Path, *, max_lag_seconds: float = 30.0) -> pd.DataFrame:
    out = frame.copy()
    outcomes: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    seen: set[tuple[str, str, str]] = set()
    for row in load_candidate_outcomes(root):
        if not is_closed_trade(row):
            continue
        address = str(row.get("address") or row.get("mint") or "").strip()
        opened = parse_event_timestamp(row.get("opened_at"))
        closed = parse_event_timestamp(row.get("closed_at") or row.get("ts_utc") or row.get("timestamp"))
        if not address or opened is None or closed is None or closed < opened:
            continue
        key = (address, opened.isoformat(), str(row.get("run_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        outcomes[address].append((opened.timestamp(), {**row, "_closed": closed}))
    for values in outcomes.values():
        values.sort(key=lambda item: item[0])
    for column in ("max_pnl_pct_seen", "outcome_closed_at"):
        if column not in out:
            out[column] = None
    matched = 0
    ambiguous = 0
    for index, feature in out.iterrows():
        address = str(feature.get("address") or feature.get("mint") or "").strip()
        stamp = parse_event_timestamp(feature.get("timestamp"))
        if stamp is None or address not in outcomes:
            continue
        values = outcomes[address]
        starts = [value[0] for value in values]
        first = bisect.bisect_left(starts, stamp.timestamp())
        candidates = [row for opened, row in values[first:] if 0 <= opened - stamp.timestamp() <= max_lag_seconds]
        # Return agreement and label persistence time prevent matching another
        # concurrent shadow/re-entry of the same mint. Unknowns are not guessed.
        realized = pd.to_numeric(feature.get("target_total_pnl_pct"), errors="coerce")
        saved = parse_event_timestamp(feature.get("ts"))
        candidates = [row for row in candidates
                      if pd.notna(realized) and pd.notna(pd.to_numeric(row.get("pnl_pct"), errors="coerce"))
                      and abs(float(row["pnl_pct"]) - float(realized)) <= max(0.0001, abs(float(realized)) * 1e-6)
                      and (saved is None or abs((saved - row["_closed"]).total_seconds()) <= 30)]
        if len(candidates) != 1:
            ambiguous += int(len(candidates) > 1)
            continue
        row = candidates[0]
        if pd.isna(feature.get("max_pnl_pct_seen")):
            peak = pd.to_numeric(row.get("max_pnl_pct_seen"), errors="coerce")
            if pd.notna(peak):
                out.at[index, "max_pnl_pct_seen"] = float(peak)
        if pd.isna(feature.get("outcome_closed_at")):
            out.at[index, "outcome_closed_at"] = row["_closed"].isoformat()
        matched += 1
    out.attrs["outcome_target_join"] = {"matched": matched, "ambiguous": ambiguous, "rows": len(out), "max_lag_seconds": max_lag_seconds}
    return out


__all__ = ["enrich_outcome_targets"]
