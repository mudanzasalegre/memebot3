from __future__ import annotations

from pathlib import Path
from typing import Any

from analytics.bucket_health import bucket_keys, summarize_bucket
from analytics.report_utils import load_candidate_outcomes, load_paper_positions, load_sqlite_positions, write_json
from config.config import PROJECT_ROOT


def build_hierarchical_scorecard(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for level, key in bucket_keys(row).items():
            grouped.setdefault(f"{level}:{key}", []).append(row)
    return {key: summarize_bucket(key, value).__dict__ for key, value in sorted(grouped.items())}


def write_hierarchical_scorecard(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_hierarchical_scorecard(root)
    write_json(root / "data" / "metrics" / "hierarchical_scorecard.json", report)
    return report


def sublane_allows_canary(report: dict[str, Any], *, lane: str, min_trades: int = 5, min_avg_pnl_pct: float = 0.0) -> bool:
    stats = report.get(f"lane:{lane}") or {}
    return int(stats.get("trades") or 0) >= min_trades and float(stats.get("avg_pnl_pct") or 0.0) >= min_avg_pnl_pct


__all__ = ["build_hierarchical_scorecard", "sublane_allows_canary", "write_hierarchical_scorecard"]
