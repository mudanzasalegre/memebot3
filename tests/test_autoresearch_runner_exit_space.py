from __future__ import annotations

from research_loop.candidate_generator import generate_candidate_policies
from research_loop.spaces import runner_exit


def test_runner_exit_space_optimizes_ladder_and_floors() -> None:
    space = runner_exit.build_space()

    assert runner_exit.safety_caps_ok()
    assert "BIRD_TP1_PCT" in space.parameters
    assert "RUNNER_FLOOR_PEAK_1000" in space.parameters
    assert "runner_capture_ratio" in runner_exit.optimization_targets()


def test_runner_exit_space_generates_safe_candidate() -> None:
    candidate = generate_candidate_policies(
        space_name="runner_exit",
        n=1,
        mode="grid",
        created_at_utc="2026-06-04T00:00:00+00:00",
    )[0]

    assert candidate["live_allowed"] is False
    assert "runner_exit" in candidate["target_lanes"]
