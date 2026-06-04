from __future__ import annotations

from research_loop.candidate_generator import generate_candidate_policies
from research_loop.spaces import lane_sizing


def test_lane_sizing_space_respects_safety_caps() -> None:
    space = lane_sizing.build_space()

    assert lane_sizing.safety_caps_ok()
    assert max(space.parameters["RESEARCH_RANK_CANARY_SIZE_SOL"]) <= 0.03
    assert max(space.parameters["SNIPER_RESEARCH_SIZE_SOL"]) <= 0.005
    assert max(space.parameters["SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL"]) <= 0.005
    assert max(space.parameters["MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL"]) <= 0.005
    assert max(space.parameters["PAPER_EXPLORATION_AMOUNT_SOL"]) <= 0.01


def test_lane_sizing_space_generates_safe_candidate() -> None:
    candidate = generate_candidate_policies(
        space_name="lane_sizing",
        n=1,
        mode="grid",
        created_at_utc="2026-06-04T00:00:00+00:00",
    )[0]

    assert candidate["live_allowed"] is False
    assert "paper_exploration" in candidate["target_lanes"]
