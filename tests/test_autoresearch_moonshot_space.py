from __future__ import annotations

from research_loop.candidate_generator import generate_candidate_policies
from research_loop.spaces import moonshot_micro


def test_moonshot_space_optimizes_expected_keys_and_caps() -> None:
    space = moonshot_micro.build_space()

    assert moonshot_micro.safety_caps_ok()
    assert "MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL" in space.parameters
    assert max(space.parameters["MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL"]) <= 0.005
    assert "moonshot_peak1000_capture" in moonshot_micro.optimization_targets()


def test_moonshot_space_generates_safe_candidate() -> None:
    candidate = generate_candidate_policies(
        space_name="moonshot_micro",
        n=1,
        mode="grid",
        created_at_utc="2026-06-04T00:00:00+00:00",
    )[0]

    assert candidate["live_allowed"] is False
    assert "pump_early_moonshot_micro_lottery" in candidate["target_lanes"]
