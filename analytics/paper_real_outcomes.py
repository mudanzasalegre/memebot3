from __future__ import annotations

import collections
import datetime as dt
import statistics
from pathlib import Path
from typing import Any

from analytics.report_utils import (
    boolish,
    dedupe_position_rows,
    fnum,
    is_severe_exit,
    load_paper_positions,
    load_sqlite_positions,
    load_deduped_positions,
    is_closed_trade,
    metrics_dir,
    write_json,
)
from config.config import PROJECT_ROOT
from analytics.forward_evidence import _costed_close


REPORT_JSON = "paper_real_outcomes.json"


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _lane(row: dict[str, Any]) -> str:
    return str(_first(row, "entry_lane", "lane", "profit_lane_tier", "size_bucket") or "unknown").strip().lower() or "unknown"


def _closed(row: dict[str, Any]) -> bool:
    return is_closed_trade(row)


def _pnl_pct(row: dict[str, Any]) -> float:
    return fnum(
        _first(
            row,
            "total_pnl_pct",
            "realized_pnl_pct",
            "pnl_pct",
            "return_pct",
        ),
        0.0,
    )


def _pnl_usd(row: dict[str, Any]) -> float:
    return fnum(_first(row, "total_pnl_usd", "realized_pnl_usd", "pnl_usd"), 0.0)


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [row for row in rows if _closed(row)]
    pnl_values = [_pnl_pct(row) for row in closed]
    wins = [value for value in pnl_values if value > 0]
    net = [result[0] for row in closed if (result := _costed_close(row)) is not None]
    return {
        "rows": len(rows),
        "open": sum(1 for row in rows if not _closed(row)),
        "closed": len(closed),
        "win_rate_pct": round(100.0 * len(wins) / len(closed), 4) if closed else 0.0,
        "avg_pnl_pct": round(statistics.fmean(pnl_values), 6) if pnl_values else 0.0,
        "median_pnl_pct": round(statistics.median(pnl_values), 6) if pnl_values else 0.0,
        "total_pnl_usd": round(sum(_pnl_usd(row) for row in closed), 8),
        "costed_closed_trades": len(net),
        "net_total_pnl_usd": round(sum(net), 8) if closed and len(net) == len(closed) else None,
        "cost_basis": "validated_estimated_paper_fills" if closed and len(net) == len(closed) else "unknown_or_incomplete",
        "severe_losses": sum(1 for row in closed if is_severe_exit(row)),
    }


def build_paper_real_outcomes_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    json_rows = load_paper_positions(root)
    sqlite_rows = load_sqlite_positions(root)
    rows = load_deduped_positions(root)

    by_lane: dict[str, dict[str, Any]] = {}
    lane_rows: collections.defaultdict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        lane_rows[_lane(row)].append(row)
    for lane, lane_group in lane_rows.items():
        by_lane[lane] = _stats(lane_group)

    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "summary": _stats(rows),
        "by_lane": by_lane,
        "sources": {
            "paper_portfolio": len(json_rows),
            "sqlite_positions": len(sqlite_rows),
            "deduped_rows": len(rows),
        },
    }


def write_paper_real_outcomes_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_paper_real_outcomes_report(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    return report


__all__ = ["REPORT_JSON", "build_paper_real_outcomes_report", "write_paper_real_outcomes_report"]
