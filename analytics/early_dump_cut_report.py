from __future__ import annotations

import json
import statistics
from itertools import product
from pathlib import Path
from typing import Any

from analytics.lane_policy_categories import classify_policy_category
from analytics.report_utils import (
    fnum,
    load_candidate_outcomes,
    load_paper_positions,
    load_sqlite_positions,
    metrics_dir,
    write_json,
    write_markdown,
)
from config.config import CFG, PROJECT_ROOT


def _csv_floats(value: Any, default: tuple[float, ...]) -> tuple[float, ...]:
    raw = str(value or "").strip()
    if not raw:
        return default
    out: list[float] = []
    for item in raw.split(","):
        try:
            out.append(float(item.strip()))
        except Exception:
            continue
    return tuple(out) or default


def _csv_ints(value: Any, default: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(int(value) for value in _csv_floats(value, tuple(float(item) for item in default))) or default


def _pnl(row: dict[str, Any]) -> float:
    return fnum(row.get("realized_pnl_pct") or row.get("total_pnl_pct") or row.get("pnl_pct") or row.get("target_total_pnl_pct"), 0.0)


def _peak(row: dict[str, Any]) -> float:
    return fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct") or row.get("max_pnl_pct"), _pnl(row))


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_pnl(row) for row in rows]
    if not pnls:
        return {
            "count": 0,
            "avg_loss": 0.0,
            "median_loss": 0.0,
            "saved_loss_estimate": 0.0,
            "false_cut_runners": 0,
        }
    return {
        "count": len(rows),
        "avg_loss": round(sum(pnls) / len(pnls), 3),
        "median_loss": round(statistics.median(pnls), 3),
        "saved_loss_estimate": round(sum(max(-12.0 - pnl, 0.0) for pnl in pnls), 3),
        "false_cut_runners": sum(1 for row in rows if _peak(row) >= 50.0),
    }


def _baseline_comparison(root: Path) -> dict[str, Any]:
    path = metrics_dir(root) / "post_run_48h_baseline.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    stats = ((payload.get("by_exit_reason") or {}).get("EARLY_DUMP_CUT") or {}) if isinstance(payload, dict) else {}
    return {
        "baseline_count": stats.get("count"),
        "baseline_avg_pnl_pct": stats.get("avg_pnl_pct"),
        "baseline_median_pnl_pct": stats.get("median_pnl_pct"),
        "baseline_severe_loss_count": stats.get("severe_loss_count"),
    }


def build_early_dump_cut_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    all_rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    rows = [row for row in all_rows if str(row.get("exit_reason") or row.get("reason") or "").upper() == "EARLY_DUMP_CUT"]
    by_lane: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_lane.setdefault(classify_policy_category(row), []).append(row)

    pnl_thresholds = _csv_floats(getattr(CFG, "EARLY_DUMP_CUT_PNL_THRESHOLDS", "-8,-10,-12"), (-8.0, -10.0, -12.0))
    after_values = _csv_floats(getattr(CFG, "EARLY_DUMP_CUT_AFTER_S_VALUES", "25,35,45"), (25.0, 35.0, 45.0))
    confirm_values = _csv_ints(getattr(CFG, "EARLY_DUMP_CUT_CONFIRM_TICKS_VALUES", "1,2"), (1, 2))
    ignore_values = _csv_floats(getattr(CFG, "EARLY_DUMP_CUT_IGNORE_IF_PEAK_VALUES", "10,15,20"), (10.0, 15.0, 20.0))
    candidates: list[dict[str, Any]] = []
    for pnl_threshold, after_s, confirm_ticks, ignore_peak in product(
        pnl_thresholds,
        after_values,
        confirm_values,
        ignore_values,
    ):
        cuttable = [row for row in rows if _pnl(row) <= pnl_threshold and _peak(row) < ignore_peak]
        candidates.append(
            {
                "pnl_threshold": pnl_threshold,
                "after_s": after_s,
                "confirm_ticks": confirm_ticks,
                "ignore_if_peak": ignore_peak,
                "cuttable_count": len(cuttable),
                "saved_loss_estimate": round(sum(max(pnl_threshold - _pnl(row), 0.0) for row in cuttable), 3),
                "false_cut_runners": sum(1 for row in rows if _pnl(row) <= pnl_threshold and _peak(row) >= ignore_peak),
            }
        )
    candidates.sort(key=lambda row: (row["saved_loss_estimate"], -row["false_cut_runners"]), reverse=True)
    return {
        "summary": _summary(rows),
        "comparison": _baseline_comparison(root),
        "by_policy_category": {key: _summary(value) for key, value in sorted(by_lane.items())},
        "search_space": {
            "pnl_thresholds": list(pnl_thresholds),
            "after_s": list(after_values),
            "confirm_ticks": list(confirm_values),
            "ignore_if_peak": list(ignore_values),
        },
        "candidates": candidates,
    }


def write_early_dump_cut_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_early_dump_cut_report(root)
    write_json(metrics_dir(root) / "early_dump_cut_report.json", report)
    lines = [
        "# Early Dump Cut Report",
        "",
        f"- Count: `{report['summary']['count']}`",
        f"- Avg loss: `{report['summary']['avg_loss']:.2f}%`",
        f"- Median loss: `{report['summary']['median_loss']:.2f}%`",
        f"- Saved loss estimate @ -12: `{report['summary']['saved_loss_estimate']:.2f}`",
        f"- False cut runners: `{report['summary']['false_cut_runners']}`",
        "",
        "## Top Candidates",
        "",
        "| PnL | After s | Confirm | Ignore peak | Cuttable | Saved est | False runners |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["candidates"][:20]:
        lines.append(
            f"| {row['pnl_threshold']:.0f} | {row['after_s']:.0f} | {row['confirm_ticks']} | {row['ignore_if_peak']:.0f} | "
            f"{row['cuttable_count']} | {row['saved_loss_estimate']:.2f} | {row['false_cut_runners']} |"
        )
    write_markdown(root / "docs" / "EARLY_DUMP_CUT_REPORT.md", lines)
    return report


__all__ = ["build_early_dump_cut_report", "write_early_dump_cut_report"]
