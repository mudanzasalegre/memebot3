from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

from analytics.strategy_proposal_validator import validate_strategy_proposal
from execution.trade_decision import TradeDecision, TradeDecisionScores
from features.decision_store import append_decision, read_decisions
from ml.feature_sets import feature_set, validate_feature_set
from ml.label_builder import build_labels
from runtime.dynamic_thresholds import build_dynamic_thresholds
from runtime.live_canary_v2 import evaluate_live_canary_v2
from runtime.policy_modes import action_for_mode, mode_for_lane
from runtime.policy_score import compute_policy_score


def test_label_builder_outputs_specialized_targets() -> None:
    labels = build_labels(
        pd.DataFrame(
            {
                "target_total_pnl_pct": [12.0, -55.0],
                "max_pnl_pct_seen": [120.0, 10.0],
                "max_pnl_after_seen_3m": [40.0, -5.0],
            }
        )
    )
    assert labels.loc[0, "is_winner"] == 1
    assert labels.loc[1, "severe_loss_50"] == 1
    assert labels.loc[0, "runner_100"] == 1
    assert labels.loc[0, "continuation_positive_after_seen"] == 1


def test_feature_sets_reject_future_leakage() -> None:
    assert feature_set("risk_features")
    assert feature_set("ev_features")
    assert feature_set("runner_features")
    assert feature_set("continuation_features")
    assert "future_price" in validate_feature_set(["future_price"])


def test_decision_store_roundtrip(tmp_path) -> None:
    path = tmp_path / "decision_ledger.jsonl"
    row = append_decision(
        {
            "address": "A",
            "timestamp": "2026-01-01T00:00:00Z",
            "lane": "pump_early_green_candle_sniper",
            "action": "shadow",
            "reason": "test",
            "features_snapshot": {"price_pct_5m": 42},
        },
        path=path,
    )
    assert row["decision_id"]
    assert read_decisions(path)[0]["features_snapshot"]["price_pct_5m"] == 42


def test_trade_decision_and_policy_score() -> None:
    score = compute_policy_score({"ev_pred_pct": 10, "runner100_proba": 0.5, "risk_proba_30": 0.1})
    decision = TradeDecision(
        address="A",
        lane="lane",
        action="buy",
        amount_sol=0.01,
        exit_profile="balanced",
        reason="test",
        scores=TradeDecisionScores(policy_score=score),
    )
    assert decision.to_dict()["scores"]["policy_score"] == score


def test_policy_modes_live_never_enforce_by_default(monkeypatch) -> None:
    import runtime.policy_modes as policy_modes

    monkeypatch.setattr(policy_modes, "CFG", SimpleNamespace(GREEN_SNIPER_POLICY_MODE="enforce", ALLOW_LIVE_POLICY_ENFORCE=False))
    assert mode_for_lane("pump_early_green_candle_sniper", live=True) == "shadow"
    assert action_for_mode(base_action="buy", policy_action="buy", mode="shadow") == "shadow"


def test_dynamic_thresholds_handles_empty_data(tmp_path) -> None:
    (tmp_path / "data" / "metrics").mkdir(parents=True)
    report = build_dynamic_thresholds(tmp_path)
    assert report["thresholds"] == {}


def test_strategy_proposal_validation_requires_live_gates() -> None:
    ok, errors = validate_strategy_proposal(
        {
            "proposal_id": "p1",
            "hypothesis": "x",
            "changes": {},
            "expected_effect": {},
            "required_gates": [],
            "live_allowed": True,
            "risk_notes": [],
        }
    )
    assert ok is False
    assert "live_allowed_requires_replay_paper_manual" in errors


def test_live_canary_v2_requires_manual_approval(monkeypatch) -> None:
    import runtime.live_canary_v2 as live_canary_v2

    monkeypatch.setattr(
        live_canary_v2,
        "CFG",
        SimpleNamespace(
            STRATEGY_OPTIMIZATION_LOCK=False,
            LIVE_CANARY_ENABLED=True,
            LIVE_CANARY_MAX_OPEN=1,
            LIVE_CANARY_MAX_DAILY_BUYS=3,
            LIVE_CANARY_DAILY_LOSS_CAP_SOL=0.05,
            LIVE_CANARY_SIZE_SOL=0.01,
            MIN_BUY_SOL=0.01,
            LIVE_REQUIRE_ROUTE=True,
        ),
    )
    decision = evaluate_live_canary_v2(
        {"has_jupiter_route": 1},
        candidate_policy_passed=True,
        paper_forward_passed=True,
        manual_approval=False,
        provider_health_ok=True,
    )
    assert decision.allowed is False
    assert decision.reason == "manual_approval_required"
