from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from analytics.report_utils import (
    address_of,
    fnum,
    load_candidate_outcomes,
    load_paper_positions,
    load_sqlite_positions,
    metrics_dir,
    write_json,
)
from config.config import PROJECT_ROOT


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _peak(row: dict[str, Any]) -> float:
    return max(
        fnum(_first(row, "highest_pnl_pct", "max_pnl_pct_seen", "peak_pnl_pct"), 0.0),
        fnum(_first(row, "total_pnl_pct", "realized_pnl_pct", "pnl_pct", "target_total_pnl_pct"), 0.0),
    )


def _pnl(row: dict[str, Any]) -> float:
    return fnum(_first(row, "total_pnl_pct", "realized_pnl_pct", "pnl_pct", "target_total_pnl_pct"), 0.0)


def _floor(row: dict[str, Any]) -> float | None:
    partial_count = int(fnum(_first(row, "partial_count"), 0.0))
    if partial_count <= 0 and str(_first(row, "partial_taken") or "").strip().lower() in {"1", "true", "yes", "on"}:
        partial_count = 1
    peak = _peak(row)
    floors: list[float] = []
    if partial_count >= 1:
        floors.append(5.0)
    if partial_count >= 2:
        floors.append(15.0)
    if partial_count >= 3:
        floors.append(40.0)
    if peak >= 100.0:
        floors.append(50.0)
    if peak >= 300.0:
        floors.append(150.0)
    return max(floors) if floors else None


def build_total_pnl_protection_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    protected = [
        row for row in rows if str(_first(row, "exit_reason", "reason") or "").strip().upper() == "TOTAL_PNL_PROTECTION_EXIT"
    ]
    at_risk: list[dict[str, Any]] = []
    for row in rows:
        floor = _floor(row)
        if floor is not None and _pnl(row) < floor:
            at_risk.append(
                {
                    "address": address_of(row),
                    "floor_pct": floor,
                    "total_pnl_pct": _pnl(row),
                    "peak_pct": _peak(row),
                    "partial_count": int(fnum(_first(row, "partial_count"), 0.0)),
                    "exit_reason": _first(row, "exit_reason", "reason"),
                }
            )
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "protected_exit_count": len(protected),
        "at_risk_rows": len(at_risk),
        "floors": {
            "after_tp1": 5.0,
            "after_tp2": 15.0,
            "after_tp3": 40.0,
            "after_peak_100": 50.0,
            "after_peak_300": 150.0,
        },
        "samples": at_risk[:100],
    }


def write_total_pnl_protection_report(root: Path | None = None) -> dict[str, Any]:
    report = build_total_pnl_protection_report(root)
    write_json(metrics_dir(root) / "total_pnl_protection_report.json", report)
    return report


__all__ = ["build_total_pnl_protection_report", "write_total_pnl_protection_report"]
