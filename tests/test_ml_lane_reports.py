from __future__ import annotations

import json

from ml.lane_reports import build_ml_lane_report


def test_ml_lane_report_counts_lanes(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(
        json.dumps({"entry_lane": "pump_early_sniper_research", "target_total_pnl_pct": 5}) + "\n",
        encoding="utf-8",
    )
    report = build_ml_lane_report(tmp_path)
    assert report["lanes"]["pump_early_sniper_research"]["rows"] == 1
