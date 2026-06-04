from __future__ import annotations

from research_loop.candidate_generator import generate_candidate_policies
from research_loop.spaces import shadow_followup


def test_shadow_followup_space_optimizes_trigger_and_micro_amount() -> None:
    space = shadow_followup.build_space()

    assert shadow_followup.safety_caps_ok()
    assert "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL" in space.parameters
    assert "SHADOW_FOLLOWUP_TRIGGER_PNL_3M" in space.parameters
    assert max(space.parameters["SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL"]) <= 0.005


def test_shadow_followup_space_generates_safe_candidate() -> None:
    candidate = generate_candidate_policies(
        space_name="shadow_followup",
        n=1,
        mode="grid",
        created_at_utc="2026-06-04T00:00:00+00:00",
    )[0]

    assert candidate["live_allowed"] is False
    assert "shadow_followup_micro" in candidate["target_lanes"]
