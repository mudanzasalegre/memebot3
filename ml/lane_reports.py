from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from analytics.report_utils import fnum, load_candidate_outcomes, load_paper_positions, load_sqlite_positions, metrics_dir, write_json
from config.config import PROJECT_ROOT
from ml.data_contract import normalize_ml_row


def build_ml_lane_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    source_rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    rows = []
    for row in source_rows:
        normalized = normalize_ml_row(row)
        normalized["_pnl_pct_for_report"] = (
            row.get("target_total_pnl_pct")
            if row.get("target_total_pnl_pct") is not None
            else row.get("pnl_pct")
            if row.get("pnl_pct") is not None
            else row.get("realized_pnl_pct")
            if row.get("realized_pnl_pct") is not None
            else row.get("total_pnl_pct")
        )
        rows.append(normalized)
    if not rows:
        return {"lanes": {}, "ml_green_sniper_block_enabled": False}
    frame = pd.DataFrame(rows)
    lanes: dict[str, Any] = {}
    for lane, group in frame.groupby("entry_lane", dropna=False):
        pnls = [fnum(value, 0.0) for value in group.get("_pnl_pct_for_report", pd.Series(dtype=float)).tolist()]
        lanes[str(lane)] = {
            "rows": int(len(group)),
            "positives": int(sum(1 for value in pnls if value > 0)),
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 3) if pnls else 0.0,
            "model_roles": {
                "green_sniper_model": str(lane) == "pump_early_green_candle_sniper",
                "research_rank_model": str(lane) in {"pump_early_sniper_research", "pump_early_research_rank_canary"},
                "risk_model": True,
                "ev_model": True,
            },
        }
    return {"lanes": lanes, "ml_green_sniper_block_enabled": False}


def write_ml_lane_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_ml_lane_report(root)
    write_json(metrics_dir(root) / "ml_lane_report.json", report)
    return report


__all__ = ["build_ml_lane_report", "write_ml_lane_report"]
