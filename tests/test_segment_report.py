from __future__ import annotations

import pandas as pd

from ml.segment_report import build_segment_report


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
