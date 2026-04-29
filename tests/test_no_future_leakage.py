from __future__ import annotations

import pandas as pd
import pytest

from features.builder import build_feature_vector
from ml.train import _select_feature_columns


def test_builder_rejects_future_key() -> None:
    with pytest.raises(AssertionError):
        build_feature_vector({"address": "x", "future_price": 1.0})


def test_builder_allows_t0_exit_policy_metadata() -> None:
    row = build_feature_vector(
        {
            "address": "x",
            "entry_regime": "pump_early",
            "entry_lane": "pump_early_green_candle_sniper",
            "gate_profile": "green_sniper",
            "runner_exit_profile": "green_sniper_runner",
            "exit_profile": "green_sniper_runner",
            "profit_pnl_guard_failures": "blocked_price5m_50_100",
        }
    )

    assert row["exit_profile"] == "green_sniper_runner"
    assert row["profit_pnl_guard_failures"] == "blocked_price5m_50_100"


def test_builder_still_rejects_true_exit_outcome_key() -> None:
    with pytest.raises(AssertionError):
        build_feature_vector({"address": "x", "exit_reason": "POST_PARTIAL_TRAILING"})


def test_training_excludes_outcome_columns() -> None:
    frame = pd.DataFrame(
        {
            "label": [0, 1, 0],
            "target_total_pnl_pct": [0.0, 10.0, -5.0],
            "exit_reason": [None, "tp", "sl"],
            "txns_last_5m": [1, 2, 3],
            "safe_feature": [4, 5, 6],
        }
    )
    _, x_cols, excluded = _select_feature_columns(frame)
    assert "target_total_pnl_pct" not in x_cols
    assert "exit_reason" not in x_cols
    assert "txns_last_5m" in x_cols
    assert "safe_feature" in x_cols
    assert "target_total_pnl_pct" in excluded
