from __future__ import annotations

from research_loop.candidate_generator import generate_candidate_policies
from research_loop.spaces import entry_quality


def test_entry_quality_space_combines_rank_sniper_and_paper_exploration() -> None:
    space = entry_quality.build_space()

    assert entry_quality.safety_caps_ok()
    assert "RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE" in space.parameters
    assert "SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M" in space.parameters
    assert "PAPER_IDLE_AMOUNT_SOL" in space.parameters
    assert max(space.parameters["PAPER_IDLE_AMOUNT_SOL"]) <= 0.01


def test_entry_quality_space_generates_safe_candidate() -> None:
    candidate = generate_candidate_policies(
        space_name="entry_quality",
        n=1,
        mode="local_search",
        created_at_utc="2026-06-04T00:00:00+00:00",
    )[0]

    assert candidate["live_allowed"] is False
    assert "paper_exploration" in candidate["target_lanes"]
