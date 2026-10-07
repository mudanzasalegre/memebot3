from types import SimpleNamespace

import ml.activation_policy as activation_policy


def _cfg(**overrides):
    base = {
        "ML_MIN_LANE_ROWS": 2,
        "ML_MIN_LANE_POSITIVES": 1,
        "ML_MIN_LANE_UNIQUE_TOKENS": 2,
        "ML_MIN_LANE_HOLDOUT_ROWS": 2,
        "ML_MIN_LANE_HOLDOUT_POSITIVES": 1,
        "ML_MAX_SELECTED_PNL_DEGRADATION_PCT": 0.0,
        "ML_MIN_JACKPOT_CAPTURE_RATE": 0.80,
        "ML_TUNE_PRECISION_FLOOR": 0.80,
        "ML_TUNE_MIN_REALIZED_SELECTED": 2,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _segment(threshold_result):
    return {
        "rows": 5,
        "positives": 3,
        "unique_tokens": 5,
        "holdout_rows": 5,
        "holdout_positives": 2,
        "selected_total_pnl": 30.0,
        "total_pnl_pct_points": 20.0,
        "jackpot_capture_rate": 1.0,
        "threshold_result": threshold_result,
    }


def test_lane_threshold_blocks_enforcement_when_precision_floor_missed(monkeypatch):
    monkeypatch.setattr(activation_policy, "CFG", _cfg())

    decision = activation_policy.lane_activation_decision(
        _segment(
            {
                "activation_ready": True,
                "activation_reason": "expected_pnl_positive",
                "objective_applied": "expected_pnl",
                "precision_at_picked": 0.50,
                "avg_realized_pnl_pct_at_picked": 10.0,
                "realized_selected_rows_at_picked": 3,
            }
        ),
        lane="pump_early_sniper_research",
        threshold=0.5,
    )

    assert decision["activation_ready"] is False
    assert decision["mode_recommended"] == "shadow"
    assert "precision_floor" in decision["reason"]


def test_lane_threshold_allows_research_enforce_only_after_precision_and_ev(monkeypatch):
    monkeypatch.setattr(activation_policy, "CFG", _cfg())

    decision = activation_policy.lane_activation_decision(
        _segment(
            {
                "activation_ready": True,
                "activation_reason": "precision_floor_met",
                "objective_applied": "expected_pnl_precision_floor",
                "precision_at_picked": 0.90,
                "avg_realized_pnl_pct_at_picked": 10.0,
                "realized_selected_rows_at_picked": 3,
            }
        ),
        lane="pump_early_sniper_research",
        threshold=0.5,
    )

    assert decision["activation_ready"] is True
    assert decision["mode_recommended"] == "enforce"
