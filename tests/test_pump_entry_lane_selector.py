from __future__ import annotations

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

