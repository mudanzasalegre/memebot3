from __future__ import annotations

from types import SimpleNamespace

import analytics.ml_policy as ml_policy


def _cfg(**overrides):
    base = {
        "ML_GATE_MODE": "lane_aware",
        "ML_LIVE_PROFIT_MODE": "sizing_only",
        "ML_RESEARCH_MODE": "enforce",
        "ML_UNKNOWN_LANE_MODE": "shadow",
        "ML_ALLOW_RESEARCH_LIVE": False,
        "ML_ALLOW_UNKNOWN_LIVE": False,
        "ML_RISK_VETO_ENABLED": False,
        "ML_RISK_SHADOW_ONLY": True,
        "ML_SIZING_ENABLED": True,
        "ML_SIZE_MIN_MULT": 0.25,
        "ML_SIZE_MID_MULT": 0.5,
        "ML_SIZE_MAX_MULT": 1.0,
        "ML_LIVE_PROFIT_EV_MIN": 0,
        "ML_LIVE_PROFIT_EV_SIZE_UP": 50,
        "ML_LIVE_PROFIT_PROBA_SIZE_UP": 0.3,
        "AI_THRESHOLD": 0.5,
        "GREEN_SNIPER_ML_MODE": "sizing_only",
        "GREEN_SNIPER_ML_BLOCK_ENABLED": False,
        "GREEN_SNIPER_ML_RISK_REDUCE_SIZE": True,
        "GREEN_SNIPER_RISK_CAN_VETO_LIVE": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_shadow_never_blocks(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ml_policy, "CFG", _cfg(ML_GATE_MODE="shadow"))
    monkeypatch.setattr(ml_policy, "THRESHOLDS_BY_LANE_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(ml_policy, "LEGACY_THRESHOLD_PATH", tmp_path / "missing2.json")
    d = ml_policy.decide_ml_action(token={"entry_lane": "unknown"}, feature_row={}, proba=0.0, base_rules_passed=True, dry_run=False, live=True)
    assert d.allow_buy is True


def test_live_profit_low_proba_allowed_in_sizing_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ml_policy, "CFG", _cfg())
    monkeypatch.setattr(ml_policy, "THRESHOLDS_BY_LANE_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(ml_policy, "LEGACY_THRESHOLD_PATH", tmp_path / "missing2.json")
    d = ml_policy.decide_ml_action(token={"entry_lane": "pump_early_pumpswap_profit"}, feature_row={}, proba=0.01, base_rules_passed=True, dry_run=False, live=True)
    assert d.allow_buy is True
    assert d.mode == "sizing_only"


def test_green_sniper_low_proba_does_not_block(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ml_policy, "CFG", _cfg(ML_GATE_MODE="lane_aware"))
    monkeypatch.setattr(ml_policy, "THRESHOLDS_BY_LANE_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(ml_policy, "LEGACY_THRESHOLD_PATH", tmp_path / "missing2.json")
    d = ml_policy.decide_ml_action(
        token={"entry_lane": "pump_early_green_candle_sniper"},
        feature_row={},
        proba=0.01,
        base_rules_passed=True,
        dry_run=False,
        live=True,
    )
    assert d.allow_buy is True
    assert d.mode == "sizing_only"


def test_research_high_proba_not_live_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ml_policy, "CFG", _cfg())
    monkeypatch.setattr(ml_policy, "THRESHOLDS_BY_LANE_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(ml_policy, "LEGACY_THRESHOLD_PATH", tmp_path / "missing2.json")
    d = ml_policy.decide_ml_action(token={"entry_lane": "pump_early_sniper_research"}, feature_row={}, proba=0.99, base_rules_passed=True, dry_run=False, live=True)
    assert d.allow_buy is False
    assert d.reason.startswith("research_live_disabled")


def test_research_live_allowed_when_explicitly_enabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ml_policy, "CFG", _cfg(ML_RESEARCH_MODE="shadow", ML_ALLOW_RESEARCH_LIVE=True))
    monkeypatch.setattr(ml_policy, "THRESHOLDS_BY_LANE_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(ml_policy, "LEGACY_THRESHOLD_PATH", tmp_path / "missing2.json")
    d = ml_policy.decide_ml_action(token={"entry_lane": "pump_early_sniper_research"}, feature_row={}, proba=0.0, base_rules_passed=True, dry_run=False, live=True)
    assert d.allow_buy is True
    assert d.mode == "shadow"


def test_global_enforce_blocks_below_threshold(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ml_policy, "CFG", _cfg(ML_GATE_MODE="legacy", AI_THRESHOLD=0.5))
    monkeypatch.setattr(ml_policy, "THRESHOLDS_BY_LANE_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(ml_policy, "LEGACY_THRESHOLD_PATH", tmp_path / "missing2.json")
    d = ml_policy.decide_ml_action(token={"entry_lane": "pump_early_pumpswap_profit"}, feature_row={}, proba=0.1, base_rules_passed=True, dry_run=True, live=False)
    assert d.allow_buy is False
