from __future__ import annotations

from pathlib import Path
from typing import Any

from analytics.report_utils import fnum, load_candidate_outcomes, load_paper_positions, load_sqlite_positions, metrics_dir, write_json, write_markdown
from config.config import PROJECT_ROOT


def _row_capture(row: dict[str, Any]) -> dict[str, Any]:
    realized = fnum(row.get("realized_pnl_pct") or row.get("total_pnl_pct") or row.get("pnl_pct") or row.get("target_total_pnl_pct"), 0.0)
    max_seen = fnum(row.get("max_pnl_seen") or row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct") or row.get("max_pnl_pct"), realized)
    capture_ratio = realized / max_seen if max_seen > 0 else 0.0
    return {
        "address": row.get("address") or row.get("mint"),
        "entry_lane": row.get("entry_lane") or row.get("profit_lane_tier") or "unknown",
        "max_pnl_seen": round(max_seen, 3),
        "realized_pnl": round(realized, 3),
        "capture_ratio": round(capture_ratio, 4),
        "giveback_pct": round(max(max_seen - realized, 0.0), 3),
        "partial_taken": bool(row.get("partial_taken") or row.get("partials") or row.get("partial_count")),
        "exit_reason": row.get("exit_reason") or row.get("reason"),
        "exit_profile": row.get("exit_profile") or row.get("runner_exit_profile") or "unknown",
    }


def build_runner_capture(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = [_row_capture(row) for row in load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)]
    runner_rows = [row for row in rows if fnum(row.get("max_pnl_seen"), 0.0) >= 50]
    buckets: dict[str, list[dict[str, Any]]] = {"gt_50": [], "gt_100": [], "gt_300": [], "gt_500": []}
    by_lane: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        peak = fnum(row.get("max_pnl_seen"), 0.0)
        if peak >= 50:
            buckets["gt_50"].append(row)
        if peak >= 100:
            buckets["gt_100"].append(row)
        if peak >= 300:
            buckets["gt_300"].append(row)
        if peak >= 500:
            buckets["gt_500"].append(row)
        if peak >= 50:
            by_lane.setdefault(str(row.get("entry_lane") or "unknown"), []).append(row)

    def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return {"count": 0, "avg_capture_ratio": 0.0, "avg_giveback_pct": 0.0}
        return {
            "count": len(items),
            "avg_capture_ratio": round(sum(fnum(item["capture_ratio"]) for item in items) / len(items), 4),
            "avg_giveback_pct": round(sum(fnum(item["giveback_pct"]) for item in items) / len(items), 3),
        }

    return {
        "summary": {key: summarize(value) for key, value in buckets.items()},
        "by_lane": {key: summarize(value) for key, value in sorted(by_lane.items())},
        "top_runners": sorted(runner_rows, key=lambda item: fnum(item["max_pnl_seen"]), reverse=True)[:100],
    }


def write_runner_capture_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_runner_capture(root)
    write_json(metrics_dir(root) / "runner_capture.json", report)
    lines = ["# Runner Capture", "", "| Bucket | Count | Avg capture | Avg giveback |", "|---|---:|---:|---:|"]
    for key, stats in report["summary"].items():
        lines.append(f"| {key} | {stats['count']} | {stats['avg_capture_ratio']:.3f} | {stats['avg_giveback_pct']:.2f}% |")
    lines.extend(["", "| Address | Peak | Realized | Capture | Exit |", "|---|---:|---:|---:|---|"])
    for row in report["top_runners"][:50]:
        lines.append(
            f"| {str(row.get('address'))[:10]}... | {row['max_pnl_seen']:.2f}% | {row['realized_pnl']:.2f}% | "
            f"{row['capture_ratio']:.3f} | {row.get('exit_reason')} |"
        )
    write_markdown(root / "docs" / "RUNNER_CAPTURE.md", lines)
    return report


__all__ = ["build_runner_capture", "write_runner_capture_report"]
