from __future__ import annotations

import datetime as dt
import collections
from pathlib import Path
from typing import Any

from analytics.current_run import current_run_identity, filter_current_run_rows
from analytics.report_utils import (
    address_of,
    load_candidate_outcomes,
    load_deduped_positions,
    load_runtime_events,
    metrics_dir,
    write_json,
)
from config.config import PROJECT_ROOT


REPORT_JSON = "current_run_summary.json"


def _event(row: dict[str, Any]) -> str:
    return str(row.get("event_type") or row.get("event") or row.get("action") or "").strip().lower()


def _reason(row: dict[str, Any]) -> str:
    return str(row.get("reason") or row.get("reject_reason") or row.get("blocked_reason") or "").strip()


def build_current_run_summary(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    runtime_rows = load_runtime_events(root)
    outcome_rows = load_candidate_outcomes(root)
    position_rows = load_deduped_positions(root)
    identity = current_run_identity(root, runtime_rows)
    current_run = str(identity.get("run_id") or "legacy")
    runtime_rows = filter_current_run_rows(runtime_rows, identity)
    outcome_rows = filter_current_run_rows(outcome_rows, identity)
    position_rows = filter_current_run_rows(position_rows, identity)
    raw_addresses = {address_of(row) for row in runtime_rows + outcome_rows if address_of(row)}
    strategy_decisions = [row for row in runtime_rows if _event(row) == "strategy_decision"]
    buys = [row for row in runtime_rows if _event(row) in {"buy", "bought", "paper_buy"}]
    actual_paper_buy_attempts = [row for row in runtime_rows if _event(row) == "actual_paper_buy_attempt"]
    actual_paper_buys = [row for row in runtime_rows if _event(row) == "actual_paper_buy"]
    blocked_before_buy = [row for row in runtime_rows if _event(row) == "blocked_before_buy"]
    sells = [row for row in runtime_rows if _event(row) == "execution" and str(row.get("side") or "").startswith("sell")]
    shadows = [row for row in outcome_rows + runtime_rows if "shadow" in str(row.get("action") or row.get("decision_action") or _reason(row)).lower()]
    shadow_only = [
        row
        for row in outcome_rows + runtime_rows
        if "shadow" in str(row.get("action") or row.get("decision_action") or _reason(row)).lower()
        and _event(row) != "actual_paper_buy"
    ]
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
        "current_run": identity,
        "started_at": started_at,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "raw_discovered": len(raw_addresses),
        "strategy_decisions": len(strategy_decisions),
        "buys": len(buys),
        "actual_paper_buy_attempts": len(actual_paper_buy_attempts),
        "actual_paper_buys": len(actual_paper_buys),
        "shadow_only": len(shadow_only),
        "blocked_before_buy": len(blocked_before_buy),
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
