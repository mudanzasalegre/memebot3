from __future__ import annotations

import pandas as pd

from ml.segment_report import build_segment_report, write_segment_outputs


def test_segment_report_marks_pnl_degradation() -> None:
    frame = pd.DataFrame(
        [
            {"mint": "a", "y_true": 1, "y_prob": 0.1, "target_total_pnl_pct": 150.0, "sample_type": "trade_close", "entry_lane": "pump_early_pumpswap_profit"},
            {"mint": "b", "y_true": 0, "y_prob": 0.9, "target_total_pnl_pct": -20.0, "sample_type": "trade_close", "entry_lane": "pump_early_pumpswap_profit"},
        ]
    )
    report = build_segment_report(frame, threshold=0.5)
    lane = report["segments"]["entry_lane"]["pump_early_pumpswap_profit"]
    assert lane["missed_jackpot_count"] == 1
    assert lane["accepted_loser_count"] == 1
    assert lane["do_not_enforce"] is True


def test_segment_outputs_can_stage_candidate_thresholds_without_publishing(tmp_path) -> None:
    frame = pd.DataFrame(
        [
            {
                "mint": "a",
                "y_true": 1,
                "y_prob": 0.8,
                "target_total_pnl_pct": 10.0,
                "sample_type": "trade_close",
                "entry_lane": "green_sniper",
            }
        ]
    )
    report = build_segment_report(frame, threshold=0.5)
    thresholds_path = tmp_path / "recommended_thresholds.by_lane.json"
    thresholds_path.write_text('{"global": {"threshold": 0.41}}', encoding="utf-8")

    thresholds = write_segment_outputs(
        report,
        json_path=tmp_path / "segment_report.json",
        md_path=tmp_path / "segment_report.md",
        thresholds_path=thresholds_path,
        global_result={"picked": 0.73, "activation_ready": False},
        publish_thresholds=False,
    )

    assert thresholds["global"]["threshold"] == 0.73
    assert thresholds_path.read_text(encoding="utf-8") == '{"global": {"threshold": 0.41}}'
