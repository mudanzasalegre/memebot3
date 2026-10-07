import pandas as pd

from ml.tune_threshold import tune_from_frame


def test_precision_floor_objective_does_not_fallback_to_expected_pnl_activation():
    frame = pd.DataFrame(
        {
            "y_true": [1, 0, 0, 0, 1, 0, 0, 0],
            "y_prob": [0.90, 0.85, 0.80, 0.70, 0.20, 0.10, 0.05, 0.01],
            "target_total_pnl_pct": [10, 50, 40, 30, 5, -5, -5, -5],
        }
    )

    result = tune_from_frame(
        frame,
        objective="expected_pnl_precision_floor",
        precision_floor=0.80,
        min_selected=2,
        min_realized_selected=2,
    )

    assert result["activation_ready"] is False
    assert result["objective_applied"] != "expected_pnl"
    assert result["activation_reason"] in {"precision_floor_not_met", "non_positive_expected_pnl"}
