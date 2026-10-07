from __future__ import annotations

from types import SimpleNamespace

from analytics.lane_sizing import resolve_lane_buy_amount
from ml.lane_taxonomy import (
    LANE_MOONSHOT_MICRO_LOTTERY,
    LANE_PAPER_BOOTSTRAP_MICRO,
    LANE_SHADOW_FOLLOWUP_MICRO,
)


def cfg(**overrides):
    base = {
        "LANE_SIZING_ENABLED": True,
        "LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED": True,
        "TRADE_AMOUNT_SOL": 0.1,
        "PAPER_MAX_TRADE_AMOUNT_SOL": 0.1,
        "MAX_TRADE_AMOUNT_SOL": 0.1,
        "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL": 0.003,
        "MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL": 0.001,
        "PAPER_BOOTSTRAP_AMOUNT_SOL": 0.1,
        "PAPER_BOOTSTRAP_MAX_AMOUNT_SOL": 0.1,
        "MICRO_LANE_HARD_CAP_SOL": 0.01,
        "LANE_SIZING_TRADE_AMOUNT_ALLOWLIST": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_shadow_followup_micro_not_overridden_by_fixed_trade_amount() -> None:
    decision = resolve_lane_buy_amount(
        {"entry_lane": LANE_SHADOW_FOLLOWUP_MICRO},
        computed_amount_sol=0.1,
        dry_run=True,
        live=False,
        cfg=cfg(),
    )

    assert decision.amount_sol == 0.003
    assert decision.reason == "shadow_followup_micro_size"


def test_moonshot_micro_uses_ultra_micro_size() -> None:
    decision = resolve_lane_buy_amount(
        {"entry_lane": LANE_MOONSHOT_MICRO_LOTTERY},
        computed_amount_sol=0.1,
        dry_run=True,
        live=False,
        cfg=cfg(),
    )

    assert decision.amount_sol == 0.001
    assert decision.reason == "moonshot_micro_size"


def test_fixed_trade_amount_can_be_allowlisted_for_specific_micro_lane() -> None:
    decision = resolve_lane_buy_amount(
        {"entry_lane": LANE_SHADOW_FOLLOWUP_MICRO},
        computed_amount_sol=0.1,
        dry_run=True,
        live=False,
        cfg=cfg(LANE_SIZING_TRADE_AMOUNT_ALLOWLIST=LANE_SHADOW_FOLLOWUP_MICRO),
    )

    assert decision.amount_sol == 0.1
    assert decision.reason == "fixed_paper_trade_amount"


def test_bootstrap_can_stay_standard_when_its_own_amount_is_configured() -> None:
    decision = resolve_lane_buy_amount(
        {"entry_lane": LANE_PAPER_BOOTSTRAP_MICRO},
        computed_amount_sol=0.003,
        dry_run=True,
        live=False,
        cfg=cfg(PAPER_BOOTSTRAP_AMOUNT_SOL=0.1),
    )

    assert decision.amount_sol == 0.1
    assert decision.reason == "paper_bootstrap_micro_size"
