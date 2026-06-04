from __future__ import annotations

import pytest

from research_loop.experiment_schema import CandidatePolicyValidationError
from research_loop.sandbox import CandidateSandboxError, create_candidate_sandbox


def _candidate() -> dict:
    return {
        "proposal_id": "ar_20260604_001",
        "created_at_utc": "2026-06-04T00:00:00+00:00",
        "experiment_type": "replay",
        "hypothesis": "Tune moonshot confirmation without live changes",
        "target_lanes": ["pump_early_moonshot_micro_lottery"],
        "changes": {"MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL": "75"},
        "expected_effect": {"increase_moonshot_capture": True},
        "required_gates": ["replay_positive", "api_budget_ok"],
        "api_budget_sensitive": True,
        "live_allowed": False,
        "risk_notes": ["paper only"],
    }


def test_sandbox_does_not_modify_real_env_and_writes_candidate_env(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("HELIUS_API_KEY=secret\nDRY_RUN=1\n", encoding="utf-8")

    result = create_candidate_sandbox(_candidate(), root=tmp_path, run_id="ar_test")

    assert env.read_text(encoding="utf-8") == "HELIUS_API_KEY=secret\nDRY_RUN=1\n"
    candidate_env = result.candidate_env_path.read_text(encoding="utf-8")
    assert "MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL=75" in candidate_env
    assert "DRY_RUN=1" in candidate_env
    assert "LIVE_CANARY_ENABLED=false" in candidate_env
    assert "HELIUS_API_KEY" not in candidate_env
    assert result.safety_report["ok"] is True
    assert result.config_hash


def test_sandbox_rejects_forbidden_changes(tmp_path) -> None:
    candidate = _candidate()
    candidate["changes"] = {"LIVE_CANARY_ENABLED": "true"}

    with pytest.raises((CandidatePolicyValidationError, CandidateSandboxError)):
        create_candidate_sandbox(candidate, root=tmp_path, run_id="ar_bad")


def test_sandbox_rejects_secret_changes(tmp_path) -> None:
    candidate = _candidate()
    candidate["changes"] = {"RPC_URL": "https://example.invalid"}

    with pytest.raises((CandidatePolicyValidationError, CandidateSandboxError)):
        create_candidate_sandbox(candidate, root=tmp_path, run_id="ar_secret")
