from __future__ import annotations

from analytics.lane_sizing import resolve_lane_buy_amount


def test_lane_sizing_blocks_trade_amount_fallback_for_unknown_lane() -> None:
    decision = resolve_lane_buy_amount(
        {"entry_lane": "unknown"},
        computed_amount_sol=0.1,
        dry_run=True,
        live=False,
    )

    assert decision.amount_sol == 0.005
    assert decision.fallback_blocked is True


def test_lane_sizing_caps_experimental_lanes() -> None:
    rank = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_research_rank_canary", "reason": "research_rank_canary_priority"},
        computed_amount_sol=0.1,
        dry_run=True,
        live=False,
    )
    moonshot = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_moonshot_micro_lottery"},
        computed_amount_sol=0.1,
        dry_run=True,
        live=False,
    )
    followup = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_shadow_followup_micro"},
        computed_amount_sol=0.1,
        dry_run=True,
        live=False,
    )

    assert rank.amount_sol == 0.02
    assert rank.amount_sol <= 0.03
    assert moonshot.amount_sol == 0.001
    assert followup.amount_sol == 0.003


def test_lane_sizing_keeps_rank_paper_normal_micro_size() -> None:
    decision = resolve_lane_buy_amount(
        {"entry_lane": "pump_early_research_rank_canary", "reason": "research_rank_canary_paper_normal"},
        computed_amount_sol=0.002,
        dry_run=True,
        live=False,
    )

    assert decision.amount_sol == 0.002
    assert decision.reason == "rank_paper_normal_size"
