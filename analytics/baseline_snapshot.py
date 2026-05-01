from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from analytics import missed_pumps
from analytics.report_utils import (
    fnum,
    load_candidate_outcomes,
    load_paper_positions,
    load_runtime_events,
    load_sqlite_positions,
    mcap_bucket,
    metrics_dir,
    price5m_bucket,
    rank_bucket,
    write_json,
    write_markdown,
)
from analytics.reporting import build_baseline_snapshot as build_existing_baseline_snapshot
from config.config import PROJECT_ROOT


def _pnl(row: dict[str, Any]) -> float:
    return fnum(row.get("realized_pnl_pct") or row.get("total_pnl_pct") or row.get("pnl_pct") or row.get("target_total_pnl_pct"), 0.0)


def _peak(row: dict[str, Any]) -> float:
    return fnum(row.get("max_pnl_seen") or row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct") or row.get("max_pnl_pct"), _pnl(row))


def _group(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = str(row.get(key) or "unknown")
        buckets[value].append(_pnl(row))
    return {
        name: {
            "count": len(values),
            "win_rate": round(100.0 * sum(1 for value in values if value > 0) / len(values), 3) if values else 0.0,
            "avg_pnl": round(sum(values) / len(values), 3) if values else 0.0,
            "median_pnl": round(sorted(values)[len(values) // 2], 3) if values else 0.0,
            "total_pnl": round(sum(values), 3),
            "severe_loss_count": sum(1 for value in values if value <= -25),
        }
        for name, values in sorted(buckets.items())
    }


def _bucket_group(rows: list[dict[str, Any]], bucket_fn, value_keys: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        raw = None
        for key in value_keys:
            if row.get(key) is not None:
                raw = row.get(key)
                break
        buckets[bucket_fn(raw)].append(_pnl(row))
    return {
        name: {
            "count": len(values),
            "win_rate": round(100.0 * sum(1 for value in values if value > 0) / len(values), 3) if values else 0.0,
            "avg_pnl": round(sum(values) / len(values), 3) if values else 0.0,
            "total_pnl": round(sum(values), 3),
        }
        for name, values in sorted(buckets.items())
    }


def build_current_baseline_snapshot(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    pnls = [_pnl(row) for row in rows]
    missed_rows = missed_pumps.build_missed_pumps(root) if hasattr(missed_pumps, "build_missed_pumps") else []
    runtime_events = load_runtime_events(root)
    return {
        "project_root": str(root),
        "base": build_existing_baseline_snapshot(),
        "runtime_events": {"rows": len(runtime_events)},
        "trades": {
            "rows": len(rows),
            "closed_or_outcome_rows": len([row for row in rows if row.get("closed") or row.get("closed_at") or row.get("pnl_pct") is not None or row.get("total_pnl_pct") is not None or row.get("realized_pnl_pct") is not None]),
            "open_rows": len([row for row in rows if not row.get("closed") and not row.get("closed_at") and row.get("pnl_pct") is None and row.get("total_pnl_pct") is None and row.get("realized_pnl_pct") is None]),
            "win_rate": round(100.0 * sum(1 for value in pnls if value > 0) / len(pnls), 3) if pnls else 0.0,
            "avg_pnl": round(sum(pnls) / len(pnls), 3) if pnls else 0.0,
            "median_pnl": round(sorted(pnls)[len(pnls) // 2], 3) if pnls else 0.0,
            "total_pnl": round(sum(pnls), 3),
            "severe_loss_count": sum(1 for value in pnls if value <= -25),
            "runner_count_50": sum(1 for row in rows if _peak(row) >= 50),
            "runner_count_100": sum(1 for row in rows if _peak(row) >= 100),
            "runner_count_300": sum(1 for row in rows if _peak(row) >= 300),
            "runner_count_500": sum(1 for row in rows if _peak(row) >= 500),
        },
        "by_lane": _group(rows, "entry_lane"),
        "by_gate_profile": _group(rows, "gate_profile"),
        "by_exit_reason": _group(rows, "exit_reason"),
        "by_reason": _group(rows, "reason"),
        "by_rank_bucket": _bucket_group(rows, rank_bucket, ("rank_score", "research_rank_score")),
        "by_price5m_bucket": _bucket_group(rows, price5m_bucket, ("price_pct_5m", "buy_price_pct_5m")),
        "by_mcap_bucket": _bucket_group(rows, mcap_bucket, ("market_cap_usd", "buy_market_cap_usd")),
        "missed_pumps": {
            "rows": len(missed_rows),
            "by_classification": _group(missed_rows, "classification"),
        },
    }


def write_current_baseline_snapshot(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    snapshot = build_current_baseline_snapshot(root)
    write_json(metrics_dir(root) / "current_baseline_snapshot.json", snapshot)
    lines = [
        "# Current Baseline",
        "",
        f"- Rows: `{snapshot['trades']['rows']}`",
        f"- Win rate: `{snapshot['trades']['win_rate']}`",
        f"- Avg PnL: `{snapshot['trades']['avg_pnl']}`",
        f"- Total PnL: `{snapshot['trades']['total_pnl']}`",
        "",
        "## By Lane",
        "",
        "| Lane | Count | Win rate | Avg PnL | Total PnL |",
        "|---|---:|---:|---:|---:|",
    ]
    for lane, stats in snapshot["by_lane"].items():
        lines.append(f"| {lane} | {stats['count']} | {stats['win_rate']:.2f}% | {stats['avg_pnl']:.2f}% | {stats['total_pnl']:.2f} |")
    lines.extend(["", "## By Price 5m Bucket", "", "| Bucket | Count | Win rate | Avg PnL |", "|---|---:|---:|---:|"])
    for bucket, stats in snapshot["by_price5m_bucket"].items():
        lines.append(f"| {bucket} | {stats['count']} | {stats['win_rate']:.2f}% | {stats['avg_pnl']:.2f}% |")
    write_markdown(root / "docs" / "CURRENT_BASELINE.md", lines)
    return snapshot


__all__ = ["build_current_baseline_snapshot", "write_current_baseline_snapshot"]
