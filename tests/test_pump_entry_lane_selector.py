from __future__ import annotations

from analytics.shadow_followup_micro import apply_shadow_followup_micro_context, evaluate_shadow_followup_micro
from analytics.pump_entry_lane_selector import select_pump_entry_lane


def test_selector_blocks_strict_without_sublane() -> None:
    decision = select_pump_entry_lane(
        {
            "entry_regime": "pump_early",
            "entry_lane": "pump_early_pumpswap_prime",
            "gate_profile": "pumpswap_prime_strict",
            "market_cap_usd": 60_000,
        }
    )

    assert decision.allowed is False
    assert decision.reason == "pumpswap_strict_no_sublane"


def test_selector_allows_rank_priority_over_mcap_momentum_block() -> None:
    decision = select_pump_entry_lane(
        {
            "entry_regime": "pump_early",
            "entry_lane": "pump_early_sniper_research",
            "gate_profile": "research_rank_canary",
            "rank_score": 72,
            "txns_last_5m": 1200,
            "liquidity_usd": 22_000,
            "market_cap_usd": 110_000,
            "has_jupiter_route": True,
            "liquidity_is_proxy": 0,
        }
    )

    assert decision.allowed is True
    assert decision.reason == "research_rank_canary_priority"


def test_selector_blocks_untagged_and_cluster_bad() -> None:
    untagged = select_pump_entry_lane({"entry_regime": "pump_early"})
    cluster = select_pump_entry_lane(
        {
            "entry_regime": "pump_early",
            "entry_lane": "pump_early_sniper_research",
            "gate_profile": "sniper_research_momentum_ignition",
            "entry_subprofile": "sniper_research_momentum_ignition",
            "reason": "confirmed",
            "cluster_bad": True,
        }
    )

    assert untagged.reason == "untagged_buy_blocked"
    assert cluster.reason == "cluster_bad_shadow_only"


def test_selector_allows_shadow_followup_context() -> None:
    row = {
        "entry_regime": "pump_early",
        "shadow_pnl_pct": 30,
        "minutes_since_first_seen": 2,
        "market_cap_usd": 70_000,
        "has_jupiter_route": True,
    }
    decision = evaluate_shadow_followup_micro(row)
    apply_shadow_followup_micro_context(row, decision)

    selected = select_pump_entry_lane(row)

    assert selected.allowed is True
    assert selected.selected_lane == "pump_early_shadow_followup_micro"
    assert selected.reason == "shadow_followup_momentum"


def test_selector_can_detect_shadow_followup_without_preapplied_lane() -> None:
    selected = select_pump_entry_lane(
        {
            "entry_regime": "pump_early",
            "shadow_pnl_pct": 30,
            "minutes_since_first_seen": 2,
            "market_cap_usd": 70_000,
            "has_jupiter_route": True,
        }
    )

    assert selected.allowed is True
    assert selected.selected_lane == "pump_early_shadow_followup_micro"


def test_selector_detects_real_liquidity_breakout_without_preapplied_lane() -> None:
    selected = select_pump_entry_lane(
        {
            "entry_regime": "pump_early",
            "liquidity_usd": 19_031,
            "liquidity_is_proxy": 0,
            "has_jupiter_route": True,
            "txns_last_5m": 866,
            "volume_24h_usd": 75_636,
            "market_cap_usd": 69_261,
            "age_minutes": 9.2,
            "price_impact_pct": 7.33,
            "price_pct_5m": 8.8,
            "rank_score": 60.3,
        }
    )

    assert selected.allowed is True
    assert selected.selected_lane == "pump_early_shadow_followup_micro"


def test_selector_allows_sniper_research_micro_fallback() -> None:
    selected = select_pump_entry_lane(
        {
            "entry_regime": "pump_early",
            "entry_lane": "pump_early_sniper_research_micro_fallback",
            "gate_profile": "sniper_research_micro_fallback",
            "profit_lane_tier": "pump_early_sniper_research_micro_fallback",
            "reason": "sniper_research_micro_fallback",
            "market_cap_usd": 80_000,
            "amount_sol": 0.003,
        }
    )

    assert selected.allowed is True
    assert selected.selected_lane == "pump_early_sniper_research_micro_fallback"
    assert selected.reason == "sniper_research_micro_fallback"
    assert selected.amount_cap_sol == 0.003
