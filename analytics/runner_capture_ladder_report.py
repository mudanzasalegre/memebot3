from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from analytics.bird_runner_exit import configured_bird_runner_steps, simulate_bird_runner_capture
from analytics.report_utils import (
    address_of,
    fnum,
    load_candidate_outcomes,
    load_paper_positions,
    load_sqlite_positions,
    metrics_dir,
    write_json,
    write_markdown,
)
from config.config import CFG, PROJECT_ROOT


def _peak_pct(row: dict[str, Any], final_pnl: float) -> float:
    return max(
        final_pnl,
        fnum(row.get("highest_pnl_pct"), 0.0),
        fnum(row.get("max_pnl_pct_seen"), 0.0),
        fnum(row.get("max_pnl_seen"), 0.0),
        fnum(row.get("peak_pnl_pct"), 0.0),
        fnum(row.get("max_pnl_pct"), 0.0),
    )


def _final_pnl_pct(row: dict[str, Any]) -> float:
    return fnum(
        row.get("realized_pnl_pct")
        or row.get("total_pnl_pct")
        or row.get("pnl_pct")
        or row.get("target_total_pnl_pct")
        or row.get("unrealized_pnl_pct"),
        0.0,
    )


def _row_result(row: dict[str, Any], source: str) -> dict[str, Any]:
    final_pnl = _final_pnl_pct(row)
    peak = _peak_pct(row, final_pnl)
    sim = simulate_bird_runner_capture(peak, final_pnl, cfg=CFG)
    current_capture = final_pnl / peak if peak > 0 else 0.0
    return {
        "source": source,
        "address": address_of(row),
        "symbol": row.get("symbol") or row.get("ticker"),
        "entry_lane": row.get("entry_lane") or row.get("profit_lane_tier") or "unknown",
        "gate_profile": row.get("gate_profile") or row.get("profit_gate") or "unknown",
        "exit_reason": row.get("exit_reason") or row.get("reason"),
        "peak_pct": round(peak, 4),
        "final_pnl_pct": round(final_pnl, 4),
        "current_capture_ratio": round(max(0.0, current_capture), 4),
        "current_giveback_pct": round(max(0.0, peak - final_pnl), 4),
        "simulated_realized_pnl_pct": sim["simulated_realized_pnl_pct"],
        "simulated_capture_ratio": sim["capture_ratio"],
        "simulated_giveback_pct": sim["giveback_pct"],
        "partials_triggered": int(sim["partials_triggered"]),
        "emergency_sell": bool(sim["emergency_sell"]),
        "moonbag_fraction": sim["moonbag_fraction"],
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "rows": 0,
            "avg_peak_pct": 0.0,
            "avg_current_capture_ratio": 0.0,
            "avg_simulated_capture_ratio": 0.0,
            "avg_current_giveback_pct": 0.0,
            "avg_simulated_realized_pnl_pct": 0.0,
            "emergency_sells": 0,
        }
    return {
        "rows": len(rows),
        "avg_peak_pct": round(sum(fnum(row["peak_pct"]) for row in rows) / len(rows), 4),
        "avg_current_capture_ratio": round(sum(fnum(row["current_capture_ratio"]) for row in rows) / len(rows), 4),
        "avg_simulated_capture_ratio": round(sum(fnum(row["simulated_capture_ratio"]) for row in rows) / len(rows), 4),
        "avg_current_giveback_pct": round(sum(fnum(row["current_giveback_pct"]) for row in rows) / len(rows), 4),
        "avg_simulated_realized_pnl_pct": round(
            sum(fnum(row["simulated_realized_pnl_pct"]) for row in rows) / len(rows),
            4,
        ),
        "emergency_sells": sum(1 for row in rows if row.get("emergency_sell")),
    }


def build_runner_capture_ladder_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    raw_rows: list[tuple[str, dict[str, Any]]] = []
    raw_rows.extend(("sqlite", row) for row in load_sqlite_positions(root))
    raw_rows.extend(("paper_portfolio", row) for row in load_paper_positions(root))
    raw_rows.extend(("candidate_outcome", row) for row in load_candidate_outcomes(root))

    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    for source, row in raw_rows:
        addr = address_of(row)
        key = (source, addr or str(id(row)))
        if key in seen:
            continue
        seen.add(key)
        result = _row_result(row, source)
        if result["peak_pct"] >= 25.0:
            rows.append(result)

    by_peak = {
        "peak_25_plus": [row for row in rows if fnum(row["peak_pct"]) >= 25.0],
        "peak_100_plus": [row for row in rows if fnum(row["peak_pct"]) >= 100.0],
        "peak_300_plus": [row for row in rows if fnum(row["peak_pct"]) >= 300.0],
        "peak_700_plus": [row for row in rows if fnum(row["peak_pct"]) >= 700.0],
    }
    by_lane: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_lane.setdefault(str(row.get("entry_lane") or "unknown"), []).append(row)

    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "enabled": bool(getattr(CFG, "BIRD_RUNNER_MULTI_PARTIAL_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED", False)),
            "steps": [step.__dict__ for step in configured_bird_runner_steps(CFG)],
            "moonbag_fraction": float(getattr(CFG, "BIRD_MOONBAG_FRACTION", 0.03) or 0.03),
            "emergency_giveback_enabled": bool(getattr(CFG, "RUNNER_GIVEBACK_EMERGENCY_ENABLED", True)),
            "emergency_giveback_live_enabled": bool(
                getattr(CFG, "RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED", False)
            ),
        },
        "summary": _summarize(rows),
        "by_peak_bucket": {name: _summarize(bucket_rows) for name, bucket_rows in by_peak.items()},
        "by_lane": {name: _summarize(bucket_rows) for name, bucket_rows in sorted(by_lane.items())},
        "top_runner_preview": sorted(rows, key=lambda row: fnum(row["peak_pct"]), reverse=True)[:100],
    }


def write_runner_capture_ladder_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_runner_capture_ladder_report(root)
    write_json(metrics_dir(root) / "runner_capture_ladder_report.json", report)

    lines = [
        "# Bird Runner Multi Partial",
        "",
        "Paper/dry-run ladder for runner capture. Live activation remains disabled by config.",
        "",
        "| Bucket | Rows | Avg peak | Current capture | Sim capture | Emergency sells |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, stats in report["by_peak_bucket"].items():
        lines.append(
            f"| {name} | {stats['rows']} | {stats['avg_peak_pct']:.2f}% | "
            f"{stats['avg_current_capture_ratio']:.3f} | {stats['avg_simulated_capture_ratio']:.3f} | "
            f"{stats['emergency_sells']} |"
        )
    lines.extend(["", "| Address | Lane | Peak | Final | Sim realized | Emergency |", "|---|---|---:|---:|---:|---|"])
    for row in report["top_runner_preview"][:50]:
        lines.append(
            f"| {str(row.get('address') or '')[:12]} | {row.get('entry_lane')} | "
            f"{row['peak_pct']:.2f}% | {row['final_pnl_pct']:.2f}% | "
            f"{row['simulated_realized_pnl_pct']:.2f}% | {row['emergency_sell']} |"
        )
    write_markdown(root / "docs" / "BIRD_RUNNER_MULTI_PARTIAL.md", lines)
    return report


__all__ = ["build_runner_capture_ladder_report", "write_runner_capture_ladder_report"]
