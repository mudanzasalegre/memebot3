from __future__ import annotations

import datetime as dt
import collections
from pathlib import Path
from typing import Any

from analytics.current_run import current_run_identity, filter_current_run_rows
from analytics.forward_evidence import _costed_close
from analytics.report_utils import (
    address_of,
    boolish,
    fnum,
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


def _closed(row: dict[str, Any]) -> bool:
    return boolish(row.get("closed"), False) or row.get("closed_at") is not None


def _pnl_usd(row: dict[str, Any]) -> float:
    for key in ("total_pnl_usd", "realized_pnl_usd", "pnl_usd"):
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return fnum(value, 0.0)
    return 0.0


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
    open_positions = [row for row in position_rows if not _closed(row)]
    closed_positions = [row for row in position_rows if _closed(row)]
    costed = [result for row in closed_positions if (result := _costed_close(row)) is not None]
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
        "closed_trades": len(closed_positions),
        "total_pnl_usd": round(sum(_pnl_usd(row) for row in closed_positions), 8),
        "costed_closed_trades": len(costed),
        "net_total_pnl_usd": round(sum(value[0] for value in costed), 8) if closed_positions and len(costed) == len(closed_positions) else None,
        "cost_basis": "validated_estimated_paper_fills" if closed_positions and len(costed) == len(closed_positions) else "unknown_or_incomplete",
    }


def write_current_run_summary(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_current_run_summary(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    return report


__all__ = ["REPORT_JSON", "build_current_run_summary", "write_current_run_summary"]
