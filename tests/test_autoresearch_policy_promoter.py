from __future__ import annotations

import pytest

from research_loop.evaluator import STATUS_ACCEPTED_REPLAY, STATUS_REJECTED
from research_loop.policy_promoter import PolicyPromotionError, promote_to_paper_candidate


def _candidate(proposal_id: str = "ar_promote_001", changes: dict | None = None) -> dict:
    return {
        "proposal_id": proposal_id,
        "created_at_utc": "2026-06-04T00:00:00+00:00",
        "experiment_type": "replay",
        "hypothesis": "Promote accepted replay candidate to paper profile",
        "target_lanes": ["pump_early_moonshot_micro_lottery"],
        "changes": changes or {"MOONSHOT_MICRO_CONFIRMATION_PNL": "75"},
        "expected_effect": {"increase_pnl": True, "increase_moonshot_capture": True},
        "required_gates": ["replay_positive", "api_budget_ok"],
        "api_budget_sensitive": True,
        "live_allowed": False,
        "risk_notes": ["paper only"],
    }


def _source_profile(tmp_path) -> None:
    profiles = tmp_path / "config" / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "paper_hotfix_runner_v2.env").write_text(
        "\n".join(
            [
                "PAPER_SNIPER_MODE=true",
                "GREEN_SNIPER_REJECT_SHADOW_ENABLED=true",
                "BIRDEYE_API_KEY=secret",
                "RPC_URL=https://example.invalid",
                "LIVE_CANARY_ENABLED=true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_promoter_creates_safe_paper_profile_without_secrets(tmp_path) -> None:
    _source_profile(tmp_path)

    result = promote_to_paper_candidate(
        _candidate(),
        evaluation_result=STATUS_ACCEPTED_REPLAY,
        root=tmp_path,
    )

    text = result.profile_path.read_text(encoding="utf-8")
    assert result.status == "paper_candidate"
    assert result.profile_name == "paper_research_candidate_ar_promote_001"
    assert "MOONSHOT_MICRO_CONFIRMATION_PNL=75" in text
    assert "DRY_RUN=1" in text
    assert "LIVE_CANARY_ENABLED=false" in text
    assert "AUTORESEARCH_AUTO_LIVE_PROMOTE=false" in text
    assert "BIRDEYE_API_KEY" not in text
    assert "RPC_URL" not in text


def test_promoter_backs_up_previous_candidate_profile(tmp_path) -> None:
    _source_profile(tmp_path)
    existing = tmp_path / "config" / "profiles" / "paper_research_candidate_ar_promote_001.env"
    existing.write_text("OLD_VALUE=1\n", encoding="utf-8")

    result = promote_to_paper_candidate(
        _candidate(),
        evaluation_result=STATUS_ACCEPTED_REPLAY,
        root=tmp_path,
    )

    assert result.backup_path is not None
    assert result.backup_path.exists()
    assert "OLD_VALUE=1" in result.backup_path.read_text(encoding="utf-8")
    assert "MOONSHOT_MICRO_CONFIRMATION_PNL=75" in result.profile_path.read_text(encoding="utf-8")


def test_promoter_rejects_non_accepted_candidate(tmp_path) -> None:
    _source_profile(tmp_path)

    with pytest.raises(PolicyPromotionError, match="candidate_must_be_accepted_replay"):
        promote_to_paper_candidate(
            _candidate(),
            evaluation_result=STATUS_REJECTED,
            root=tmp_path,
        )


def test_promoter_rejects_live_source_profile_and_secret_changes(tmp_path) -> None:
    profiles = tmp_path / "config" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "sniper_live_canary.env").write_text("DRY_RUN=0\n", encoding="utf-8")

    with pytest.raises(PolicyPromotionError, match="source_profile_must_not_be_live"):
        promote_to_paper_candidate(
            _candidate(),
            evaluation_result=STATUS_ACCEPTED_REPLAY,
            root=tmp_path,
            source_profile="sniper_live_canary",
        )

    _source_profile(tmp_path)
    with pytest.raises(Exception):
        promote_to_paper_candidate(
            _candidate("ar_secret", {"RPC_URL": "https://example.invalid"}),
            evaluation_result=STATUS_ACCEPTED_REPLAY,
            root=tmp_path,
        )
