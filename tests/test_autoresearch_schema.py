from __future__ import annotations

import json

import pytest

from research_loop.experiment_schema import CandidatePolicyValidationError, validate_candidate_policy


def _candidate() -> dict:
    return {
        "proposal_id": "ar_20260519_001",
        "created_at_utc": "2026-05-19T00:00:00+00:00",
        "experiment_type": "replay",
        "hypothesis": "Improve moonshot capture with stricter trigger",
        "target_lanes": ["pump_early_moonshot_micro_lottery"],
        "changes": {"MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL": "75"},
        "expected_effect": {
            "increase_pnl": True,
            "increase_win_rate": False,
            "increase_moonshot_capture": True,
            "reduce_severe_losses": True,
        },
        "required_gates": ["replay_positive", "api_budget_ok"],
        "api_budget_sensitive": True,
        "live_allowed": False,
        "risk_notes": ["paper only"],
    }


def test_valid_candidate_policy_passes() -> None:
    candidate = validate_candidate_policy(_candidate())

    assert candidate.proposal_id == "ar_20260519_001"
    assert candidate.live_allowed is False
    assert candidate.changes["MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL"] == "75"


def test_valid_candidate_policy_can_load_from_path(tmp_path) -> None:
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(_candidate()), encoding="utf-8")

    assert validate_candidate_policy(path).proposal_id == "ar_20260519_001"


def test_rejects_candidate_without_proposal_id() -> None:
    candidate = _candidate()
    candidate.pop("proposal_id")

    with pytest.raises(CandidatePolicyValidationError) as exc:
        validate_candidate_policy(candidate)

    assert "missing_fields:proposal_id" in str(exc.value)


def test_rejects_candidate_without_hypothesis() -> None:
    candidate = _candidate()
    candidate.pop("hypothesis")

    with pytest.raises(CandidatePolicyValidationError) as exc:
        validate_candidate_policy(candidate)

    assert "missing_fields:hypothesis" in str(exc.value)


def test_rejects_live_allowed_true() -> None:
    candidate = _candidate()
    candidate["live_allowed"] = True

    with pytest.raises(CandidatePolicyValidationError) as exc:
        validate_candidate_policy(candidate)

    assert "live_allowed_must_be_false" in str(exc.value)


def test_rejects_empty_changes() -> None:
    candidate = _candidate()
    candidate["changes"] = {}

    with pytest.raises(CandidatePolicyValidationError) as exc:
        validate_candidate_policy(candidate)

    assert "changes_must_not_be_empty" in str(exc.value)
