from __future__ import annotations

import pandas as pd
import pytest

from features.builder import build_feature_vector
from ml.train import _select_feature_columns
from analytics.sniper_research_subprofiles import apply_sniper_research_subprofile_context, evaluate_sniper_research_subprofile


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


def test_deep_reversal_context_does_not_emit_future_exit_key() -> None:
    token = {
        "address": "x",
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_sniper_research",
        "price_pct_5m": -72,
        "txns_last_5m": 650,
        "market_cap_usd": 20_000,
        "has_jupiter_route": True,
    }
    decision = evaluate_sniper_research_subprofile(token)
    apply_sniper_research_subprofile_context(token, decision)

    assert "sniper_research_defensive_exit" not in token
    row = build_feature_vector(token)
    assert row["exit_profile"] == "sniper_deep_reversal_defensive"


def test_builder_strips_legacy_sniper_defensive_exit_key() -> None:
    row = build_feature_vector(
        {
            "address": "x",
            "entry_regime": "pump_early",
            "sniper_research_defensive_exit": 1,
        }
    )

    assert "sniper_research_defensive_exit" not in row.index


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
