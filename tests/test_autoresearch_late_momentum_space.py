from __future__ import annotations

from research_loop.candidate_generator import generate_candidate_policies
from research_loop.spaces import late_momentum


def test_late_momentum_space_optimizes_confirmation_and_caps() -> None:
    space = late_momentum.build_space()

    assert late_momentum.safety_caps_ok()
    assert "LATE_MOMENTUM_MICRO_AMOUNT_SOL" in space.parameters
    assert "LATE_MOMENTUM_CONFIRMATION" in space.parameters
    assert max(space.parameters["LATE_MOMENTUM_MICRO_AMOUNT_SOL"]) <= 0.005


def test_late_momentum_space_generates_safe_candidate() -> None:
    candidate = generate_candidate_policies(
        space_name="late_momentum",
        n=1,
        mode="grid",
        created_at_utc="2026-06-04T00:00:00+00:00",
    )[0]

    assert candidate["live_allowed"] is False
    assert "late_momentum_micro" in candidate["target_lanes"]
