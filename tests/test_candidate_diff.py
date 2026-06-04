from __future__ import annotations

from research_loop.candidate_diff import build_candidate_diff, write_candidate_diff


def _candidate() -> dict:
    return {
        "proposal_id": "ar_20260604_003",
        "created_at_utc": "2026-06-04T00:00:00+00:00",
        "experiment_type": "replay",
        "hypothesis": "Tune runner and sizing",
        "target_lanes": ["pump_early_sniper_research"],
        "changes": {
            "BIRD_TP1_PCT": "25",
            "RESEARCH_RANK_CANARY_SIZE_SOL": "0.02",
        },
        "expected_effect": {"increase_pnl": True},
        "required_gates": ["replay_positive", "api_budget_ok"],
        "api_budget_sensitive": True,
        "live_allowed": False,
        "risk_notes": ["paper only"],
    }


def test_candidate_diff_shows_changes_and_impacts(tmp_path) -> None:
    diff = build_candidate_diff(_candidate())

    assert "## Env Changes" in diff
    assert "`BIRD_TP1_PCT`" in diff
    assert "## Exits Affected" in diff
    assert "## Sizing Affected" in diff
    assert "no protected API cadence/RPM keys changed" in diff

    path = write_candidate_diff(_candidate(), run_dir=tmp_path)
    assert path.exists()
