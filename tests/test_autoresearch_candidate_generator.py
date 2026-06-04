from __future__ import annotations

import json

from research_loop.candidate_generator import generate_candidate_policies, generate_research_candidates
from research_loop.experiment_schema import validate_candidate_policy
from research_loop.safety import validate_candidate_safety


def test_candidate_generator_generates_n_candidates() -> None:
    candidates = generate_candidate_policies(
        space_name="moonshot_micro",
        n=5,
        mode="seeded_random",
        seed=42,
        created_at_utc="2026-06-04T00:00:00+00:00",
    )

    assert len(candidates) == 5
    assert {candidate["proposal_id"] for candidate in candidates}


def test_candidate_generator_is_reproducible_by_seed() -> None:
    first = generate_candidate_policies(
        space_name="moonshot_micro",
        n=5,
        mode="seeded_random",
        seed=42,
        created_at_utc="2026-06-04T00:00:00+00:00",
    )
    second = generate_candidate_policies(
        space_name="moonshot_micro",
        n=5,
        mode="seeded_random",
        seed=42,
        created_at_utc="2026-06-04T00:00:00+00:00",
    )

    assert first == second


def test_candidate_generator_outputs_schema_and_safety_valid_candidates() -> None:
    candidates = generate_candidate_policies(
        space_name="rank_canary",
        n=3,
        mode="grid",
        seed=1,
        created_at_utc="2026-06-04T00:00:00+00:00",
    )

    for candidate in candidates:
        validate_candidate_policy(candidate)
        assert validate_candidate_safety(candidate).ok
        assert candidate["live_allowed"] is False
        assert "api_budget_ok" in candidate["required_gates"]


def test_candidate_generator_writes_candidate_files(tmp_path) -> None:
    candidates = generate_research_candidates(
        space_name="shadow_followup",
        n=2,
        mode="seeded_random",
        seed=7,
        root=tmp_path,
        write=True,
        created_at_utc="2026-06-04T00:00:00+00:00",
    )

    out_dir = tmp_path / "strategy_proposals" / "candidates"
    paths = sorted(out_dir.glob("*.json"))
    assert len(paths) == 2
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    assert payload["proposal_id"] in {candidate["proposal_id"] for candidate in candidates}


def test_candidate_generator_supports_local_search_and_bandit_modes() -> None:
    local = generate_candidate_policies(
        space_name="entry_quality",
        n=3,
        mode="local_search",
        seed=3,
        created_at_utc="2026-06-04T00:00:00+00:00",
    )
    bandit = generate_candidate_policies(
        space_name="lane_sizing",
        n=3,
        mode="bandit_suggested",
        seed=3,
        created_at_utc="2026-06-04T00:00:00+00:00",
    )

    assert len(local) == 3
    assert len(bandit) == 3
    assert all(validate_candidate_safety(candidate).ok for candidate in local + bandit)
