from __future__ import annotations

import json

import pytest

from research_loop.paper_forward import start_paper_forward
from research_loop.rollback import PaperRollbackError, rollback_paper_candidate


def _candidate(proposal_id: str = "ar_rb", changes: dict | None = None) -> dict:
    return {
        "proposal_id": proposal_id,
        "created_at_utc": "2026-06-04T00:00:00+00:00",
        "experiment_type": "replay",
        "hypothesis": "Rollback degraded paper candidate",
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
    profiles.mkdir(parents=True)
    (profiles / "paper_hotfix_runner_v2.env").write_text(
        "DRY_RUN=1\nPAPER_SNIPER_MODE=true\nLIVE_CANARY_ENABLED=false\n",
        encoding="utf-8",
    )


def test_rollback_restores_previous_paper_candidate_profile_and_marks_rejected(tmp_path) -> None:
    _source_profile(tmp_path)
    target = tmp_path / "config" / "profiles" / "paper_research_candidate_ar_rb.env"
    target.write_text("OLD_VALUE=1\n", encoding="utf-8")
    start = start_paper_forward(_candidate(), root=tmp_path, run_id="paper_rollback", profile_id="ar_rb")
    assert "MOONSHOT_MICRO_CONFIRMATION_PNL=75" in target.read_text(encoding="utf-8")

    result = rollback_paper_candidate(start.run_dir, root=tmp_path, reason="candidate_degraded")

    assert result.restored is True
    assert result.rollback_report_path.exists()
    assert "OLD_VALUE=1" in target.read_text(encoding="utf-8")
    state = json.loads((start.run_dir / "paper_forward_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "rejected_paper"
    assert state["rollback"]["reason"] == "candidate_degraded"


def test_rollback_refuses_live_or_non_research_profile(tmp_path) -> None:
    run_dir = tmp_path / "data" / "research_runs" / "paper_forward" / "bad"
    run_dir.mkdir(parents=True)
    live_profile = tmp_path / "config" / "profiles" / "sniper_live_canary.env"
    live_profile.parent.mkdir(parents=True)
    live_profile.write_text("DRY_RUN=0\n", encoding="utf-8")
    (run_dir / "paper_forward_state.json").write_text(
        json.dumps(
            {
                "run_id": "bad",
                "promotion": {
                    "profile_path": str(live_profile),
                    "backup_path": None,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PaperRollbackError, match="rollback_target_must_be_paper_research_candidate"):
        rollback_paper_candidate(run_dir, root=tmp_path)
