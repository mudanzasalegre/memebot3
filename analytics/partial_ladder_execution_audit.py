from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from analytics import exit_policy, runner_ladder
from analytics.report_utils import (
    address_of,
    fnum,
    inum,
    load_candidate_outcomes,
    load_paper_positions,
    load_runtime_events,
    load_sqlite_positions,
    metrics_dir,
    write_json,
)
from config.config import CFG, PROJECT_ROOT


REPORT_JSON = "partial_ladder_execution_audit.json"


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _peak(row: dict[str, Any]) -> float:
    return max(
        fnum(_first(row, "highest_pnl_pct", "max_pnl_pct_seen", "peak_pnl_pct", "observed_peak_after_seen"), 0.0),
        fnum(_first(row, "pnl_pct", "realized_pnl_pct", "total_pnl_pct"), 0.0),
    )


def _partial_count(row: dict[str, Any]) -> int:
    return max(
        inum(_first(row, "partial_count", "partials"), 0),
        1 if bool(row.get("partial_taken")) else 0,
    )


def _ladder_state(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("partial_ladder_state")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}
    return runner_ladder.state_from_subject(row)


def _executed_step_indexes(row: dict[str, Any]) -> set[int]:
    state = _ladder_state(row)
    executed = state.get("executed_steps")
    if not isinstance(executed, list):
        return set()
    out: set[int] = set()
    for item in executed:
        if isinstance(item, dict):
            idx = item.get("index")
        else:
            idx = item
        try:
            out.add(int(idx))
        except Exception:
            raw = str(idx or "").strip().lower()
            if raw.startswith("tp"):
                try:
                    out.add(int(raw[2:]))
                except Exception:
                    continue
    return out


def build_partial_ladder_execution_audit(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    runtime_rows = load_runtime_events(root)
    tp1 = float(getattr(CFG, "BIRD_TP1_PCT", 25.0) or 25.0)
    peak_rows = [row for row in rows if _peak(row) >= tp1]
    zero_partial = [row for row in peak_rows if _partial_count(row) <= 0]
    missed_tick_gap = [
        row
        for row in runtime_rows + rows
        if str(row.get("event_type") or row.get("event") or row.get("reason") or "").strip().lower()
        == "missed_partial_due_to_tick_gap"
    ]
    executed_counts = {"tp1": 0, "tp2": 0, "tp3": 0}
    for row in rows:
        executed = _executed_step_indexes(row)
        if 1 in executed or _partial_count(row) >= 1:
            executed_counts["tp1"] += 1
        if 2 in executed or _partial_count(row) >= 2:
            executed_counts["tp2"] += 1
        if 3 in executed or _partial_count(row) >= 3:
            executed_counts["tp3"] += 1
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "bird_runner_multi_partial_enabled": bool(getattr(CFG, "BIRD_RUNNER_MULTI_PARTIAL_ENABLED", True)),
            "bird_tp1_pct": tp1,
            "bird_tp1_fraction": float(getattr(CFG, "BIRD_TP1_FRACTION", 0.25) or 0.25),
            "effective_tp1_present": bool(exit_policy.partial_ladder_plan(
                {
                    "dry_run": True,
                    "entry_lane": "pump_early_research_rank_canary",
                    "gate_profile": "research_rank_canary",
                    "entry_qty": 1000,
                    "qty": 1000,
                    "realized_qty": 0,
                },
                tp1,
            ).get("sell_fraction_of_remaining")),
        },
        "positions_with_peak_above_tp1": len(peak_rows),
        "positions_with_partial_count_0": len(zero_partial),
        "missed_partial_due_to_tick_gap": len(missed_tick_gap),
        "executed_tp1": executed_counts["tp1"],
        "executed_tp2": executed_counts["tp2"],
        "executed_tp3": executed_counts["tp3"],
        "samples_zero_partial_peak_above_tp1": [
            {
                "address": address_of(row),
                "entry_lane": _first(row, "entry_lane", "lane"),
                "gate_profile": _first(row, "gate_profile", "sniper_gate_profile"),
                "peak_pnl_pct": _peak(row),
                "partial_count": _partial_count(row),
                "exit_reason": _first(row, "exit_reason", "reason"),
            }
            for row in zero_partial[:25]
        ],
    }


def write_partial_ladder_execution_audit(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_partial_ladder_execution_audit(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    return report


__all__ = ["REPORT_JSON", "build_partial_ladder_execution_audit", "write_partial_ladder_execution_audit"]
