from __future__ import annotations

from pathlib import Path
from typing import Any

from analytics.bird_runner_exit import simulate_bird_runner_capture
from analytics.report_utils import fnum, load_candidate_outcomes, load_paper_positions, load_sqlite_positions, metrics_dir, write_json
from config.config import PROJECT_ROOT


def build_runner_capture_recommendations(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    sims = []
    for row in rows:
        peak = fnum(row.get("max_pnl_pct_seen") or row.get("max_pnl_seen") or row.get("peak_pnl_pct"), 0.0)
        realized = fnum(row.get("realized_pnl_pct") or row.get("total_pnl_pct") or row.get("pnl_pct") or row.get("target_total_pnl_pct"), 0.0)
        if peak >= 50:
            sim = simulate_bird_runner_capture(peak, realized)
            sim["address"] = row.get("address") or row.get("mint")
            sim["current_realized_pnl_pct"] = realized
            sim["uplift_pct"] = round(sim["simulated_realized_pnl_pct"] - realized, 4)
            sims.append(sim)
    avg_uplift = sum(item["uplift_pct"] for item in sims) / len(sims) if sims else 0.0
    return {"runner_rows": len(sims), "avg_bird_runner_uplift_pct": round(avg_uplift, 4), "top_recommendations": sorted(sims, key=lambda item: item["uplift_pct"], reverse=True)[:50]}


def write_runner_capture_recommendations(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_runner_capture_recommendations(root)
    write_json(metrics_dir(root) / "runner_capture_optimizer.json", report)
    return report


__all__ = ["build_runner_capture_recommendations", "write_runner_capture_recommendations"]
