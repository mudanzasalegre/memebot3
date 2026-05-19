from __future__ import annotations

import collections
import datetime as dt
from pathlib import Path
from typing import Any

from analytics.report_utils import (
    address_of,
    load_candidate_outcomes,
    load_paper_positions,
    load_runtime_events,
    load_sqlite_positions,
    metrics_dir,
    write_json,
)
from config.config import PROJECT_ROOT


REPORT_JSON = "current_run_summary.json"


def _event(row: dict[str, Any]) -> str:
    return str(row.get("event_type") or row.get("event") or row.get("action") or "").strip().lower()


def _reason(row: dict[str, Any]) -> str:
    return str(row.get("reason") or row.get("reject_reason") or row.get("blocked_reason") or "").strip()


def _run_id(rows: list[dict[str, Any]]) -> str:
    ids = [str(row.get("run_id") or "").strip() for row in rows if str(row.get("run_id") or "").strip()]
    if not ids:
        return "legacy"
    return collections.Counter(ids).most_common(1)[0][0]


def build_current_run_summary(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    runtime_rows = load_runtime_events(root)
    outcome_rows = load_candidate_outcomes(root)
    position_rows = load_paper_positions(root) + load_sqlite_positions(root)
    rows = runtime_rows + outcome_rows + position_rows
    current_run = _run_id(runtime_rows or rows)
    if current_run != "legacy":
        runtime_rows = [row for row in runtime_rows if str(row.get("run_id") or "") == current_run]
        outcome_rows = [row for row in outcome_rows if str(row.get("run_id") or "") in {current_run, ""}]
    raw_addresses = {address_of(row) for row in runtime_rows + outcome_rows if address_of(row)}
    strategy_decisions = [row for row in runtime_rows if _event(row) == "strategy_decision"]
    buys = [row for row in runtime_rows if _event(row) in {"buy", "bought", "paper_buy"}]
    sells = [row for row in runtime_rows if _event(row) == "execution" and str(row.get("side") or "").startswith("sell")]
    shadows = [row for row in outcome_rows + runtime_rows if "shadow" in str(row.get("action") or row.get("decision_action") or _reason(row)).lower()]
    blockers = collections.Counter(
        reason for reason in (_reason(row) for row in runtime_rows + outcome_rows) if reason
    )
    open_positions = [row for row in position_rows if not bool(row.get("closed"))]
    closed_positions = [row for row in position_rows if bool(row.get("closed"))]
    ts_values = [str(row.get("ts_utc") or row.get("created_at") or "").strip() for row in runtime_rows if row.get("ts_utc")]
    started_at = min(ts_values) if ts_values else None
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_id": current_run,
        "started_at": started_at,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "raw_discovered": len(raw_addresses),
        "strategy_decisions": len(strategy_decisions),
        "buys": len(buys),
        "sells": len(sells),
        "shadows": len(shadows),
        "top_blockers": dict(blockers.most_common(20)),
        "open_positions": len(open_positions),
        "closed_positions": len(closed_positions),
    }


def write_current_run_summary(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_current_run_summary(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    return report


__all__ = ["REPORT_JSON", "build_current_run_summary", "write_current_run_summary"]
