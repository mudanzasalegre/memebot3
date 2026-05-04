from __future__ import annotations

from analytics.lane_policy_categories import classify_policy_category


def test_policy_category_splits_green_and_research_lanes() -> None:
    assert classify_policy_category({"entry_lane": "pump_early_green_candle_sniper"}) == "green_sniper_pure"
    assert (
        classify_policy_category(
            {
                "entry_lane": "pump_early_green_candle_sniper",
                "green_sniper_action": "shadow",
            }
        )
        == "green_sniper_shadow"
    )
    assert (
        classify_policy_category(
            {
                "entry_lane": "pump_early_green_candle_sniper",
                "gate_profile": "green_sniper_restricted_buy",
            }
        )
        == "green_sniper_restricted_buy"
    )
    assert classify_policy_category({"entry_lane": "pump_early_research_rank_canary"}) == "research_rank_canary"
    assert classify_policy_category({"entry_lane": "pump_early_late_momentum_watch"}) == "late_momentum_watch"
    assert classify_policy_category({"entry_subtype": "paper_birth_probe"}) == "paper_birth_probe"
