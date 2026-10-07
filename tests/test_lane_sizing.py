from __future__ import annotations

from types import SimpleNamespace

from analytics.lane_sizing import resolve_lane_buy_amount


def test_lane_sizing_uses_paper_default_for_unknown_lane() -> None:
    decision = resolve_lane_buy_amount(
        {"entry_lane": "unknown"},
        computed_amount_sol=0.1,
        dry_run=True,
        live=False,
    )

    assert decision.amount_sol == 0.1
    assert decision.fallback_blocked is False


def test_lane_sizing_caps_paper_default_per_trade() -> None:
    cfg = SimpleNamespace(
        DEFAULT_PAPER_BUY_SOL=0.2,
        PAPER_MAX_TRADE_AMOUNT_SOL=0.1,
        LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED=False,
    )
    decision = resolve_lane_buy_amount(
        {"entry_lane": "unknown"},
        computed_amount_sol=0.2,
        dry_run=True,
        live=False,
        cfg=cfg,
    )

    assert decision.amount_sol == 0.1
    assert decision.cap_sol == 0.1


def test_lane_sizing_uses_fixed_trade_amount_for_experimental_paper_lanes() -> None:
    cfg = SimpleNamespace(
        TRADE_AMOUNT_SOL=0.1,
        PAPER_MAX_TRADE_AMOUNT_SOL=0.1,
        LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED=True,
        SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL=0.003,
        SNIPER_RESEARCH_MICRO_FALLBACK_AMOUNT_SOL=0.003,
        MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL=0.001,
    )
    rank = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_research_rank_canary", "reason": "research_rank_canary_priority"},
        computed_amount_sol=0.005,
        dry_run=True,
        live=False,
        cfg=cfg,
    )
    moonshot = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_moonshot_micro_lottery"},
        computed_amount_sol=0.001,
        dry_run=True,
        live=False,
        cfg=cfg,
    )
    followup = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_shadow_followup_micro"},
        computed_amount_sol=0.003,
        dry_run=True,
        live=False,
        cfg=cfg,
    )
    sniper_fallback = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_sniper_research_micro_fallback"},
        computed_amount_sol=0.003,
        dry_run=True,
        live=False,
        cfg=cfg,
    )

    assert rank.amount_sol == 0.1
    assert moonshot.amount_sol == 0.001
    assert followup.amount_sol == 0.003
    assert sniper_fallback.amount_sol == 0.003
    assert sniper_fallback.reason == "sniper_research_micro_fallback_size"


def test_lane_sizing_rank_paper_normal_uses_fixed_trade_amount() -> None:
    decision = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_research_rank_canary", "reason": "research_rank_canary_paper_normal"},
        computed_amount_sol=0.002,
        dry_run=True,
        live=False,
    )

    assert decision.amount_sol == 0.1
    assert decision.reason == "exact_paper_trade_amount"


def test_lane_sizing_honors_configured_paper_exploration_amount_without_hidden_cap() -> None:
    cfg = SimpleNamespace(PAPER_IDLE_AMOUNT_SOL=0.1, PAPER_MAX_TRADE_AMOUNT_SOL=0.1)
    decision = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_paper_exploration_micro"},
        computed_amount_sol=0.1,
        dry_run=True,
        live=False,
        cfg=cfg,
    )

    assert decision.amount_sol == 0.1
    assert decision.reason == "paper_exploration_micro_size"


def test_lane_sizing_honors_configured_paper_bootstrap_amount_without_hidden_cap() -> None:
    cfg = SimpleNamespace(
        PAPER_BOOTSTRAP_AMOUNT_SOL=0.1,
        PAPER_BOOTSTRAP_MAX_AMOUNT_SOL=0.1,
        PAPER_MAX_TRADE_AMOUNT_SOL=0.1,
    )
    decision = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_paper_bootstrap_micro"},
        computed_amount_sol=0.1,
        dry_run=True,
        live=False,
        cfg=cfg,
    )

    assert decision.amount_sol == 0.1
    assert decision.reason == "paper_bootstrap_micro_size"


def test_lane_sizing_hard_caps_configured_micro_lane_amounts() -> None:
    cfg = SimpleNamespace(
        SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL=0.02,
        SNIPER_RESEARCH_MICRO_FALLBACK_AMOUNT_SOL=0.02,
        MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL=0.02,
        LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED=False,
        MICRO_LANE_HARD_CAP_SOL=0.01,
    )
    followup = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_shadow_followup_micro"},
        computed_amount_sol=0.1,
        dry_run=True,
        live=False,
        cfg=cfg,
    )
    fallback = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_sniper_research_micro_fallback"},
        computed_amount_sol=0.1,
        dry_run=True,
        live=False,
        cfg=cfg,
    )
    moonshot = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_moonshot_micro_lottery"},
        computed_amount_sol=0.1,
        dry_run=True,
        live=False,
        cfg=cfg,
    )

    assert followup.amount_sol == 0.01
    assert fallback.amount_sol == 0.01
    assert moonshot.amount_sol == 0.01
    assert followup.warning == "micro_lane_hard_cap"


def test_lane_sizing_live_micro_lane_ignores_fixed_amount() -> None:
    cfg = SimpleNamespace(
        TRADE_AMOUNT_SOL=0.1,
        MAX_TRADE_AMOUNT_SOL=0.1,
        LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED=True,
        MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL=0.001,
    )
    decision = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_moonshot_micro_lottery"},
        computed_amount_sol=0.08,
        dry_run=False,
        live=True,
        cfg=cfg,
    )

    assert decision.amount_sol == 0.001
    assert decision.reason == "moonshot_micro_size"
