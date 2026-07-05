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
    rank = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_research_rank_canary", "reason": "research_rank_canary_priority"},
        computed_amount_sol=0.005,
        dry_run=True,
        live=False,
    )
    moonshot = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_moonshot_micro_lottery"},
        computed_amount_sol=0.001,
        dry_run=True,
        live=False,
    )
    followup = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_shadow_followup_micro"},
        computed_amount_sol=0.003,
        dry_run=True,
        live=False,
    )
    sniper_fallback = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_sniper_research_micro_fallback"},
        computed_amount_sol=0.003,
        dry_run=True,
        live=False,
    )

    assert rank.amount_sol == 0.1
    assert moonshot.amount_sol == 0.1
    assert followup.amount_sol == 0.1
    assert sniper_fallback.amount_sol == 0.1
    assert sniper_fallback.reason == "fixed_paper_trade_amount"


def test_lane_sizing_rank_paper_normal_uses_fixed_trade_amount() -> None:
    decision = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_research_rank_canary", "reason": "research_rank_canary_paper_normal"},
        computed_amount_sol=0.002,
        dry_run=True,
        live=False,
    )

    assert decision.amount_sol == 0.1
    assert decision.reason == "fixed_paper_trade_amount"


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
    assert decision.reason == "fixed_paper_trade_amount"


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
    assert decision.reason == "fixed_paper_trade_amount"


def test_lane_sizing_honors_configured_experimental_lane_amounts_without_hidden_caps() -> None:
    cfg = SimpleNamespace(
        SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL=0.02,
        SNIPER_RESEARCH_MICRO_FALLBACK_AMOUNT_SOL=0.02,
        MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL=0.02,
        LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED=False,
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

    assert followup.amount_sol == 0.02
    assert fallback.amount_sol == 0.02
    assert moonshot.amount_sol == 0.02


def test_lane_sizing_live_fixed_amount_respects_wallet_limited_input() -> None:
    cfg = SimpleNamespace(
        TRADE_AMOUNT_SOL=0.1,
        MAX_TRADE_AMOUNT_SOL=0.1,
        LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED=True,
    )
    decision = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_moonshot_micro_lottery"},
        computed_amount_sol=0.08,
        dry_run=False,
        live=True,
        cfg=cfg,
    )

    assert decision.amount_sol == 0.08
    assert decision.reason == "fixed_live_trade_amount"
