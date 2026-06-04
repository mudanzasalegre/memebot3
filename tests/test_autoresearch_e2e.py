from __future__ import annotations

import json
from pathlib import Path

from research_loop.scoreboard import load_scoreboard
from research_loop.smoke import run_autoresearch_smoke


def test_autoresearch_smoke_runs_full_local_loop(tmp_path: Path) -> None:
    result = run_autoresearch_smoke(
        root=tmp_path,
        smoke_id="ar_smoke_test",
        seed=7,
    )

    assert result.status == "ok"
    assert result.candidates_generated == 3
    assert len(result.results) == 3
    assert result.live_remains_false is True
    assert result.api_budget_path is not None
    assert result.api_budget_path.exists()
    assert result.scoreboard_path is not None
    assert result.scoreboard_path.exists()
    assert result.paper_profile_path is not None
    assert result.paper_profile_path.exists()
    assert result.report_path is not None
    assert result.report_path.exists()

    profile_text = result.paper_profile_path.read_text(encoding="utf-8")
    assert "DRY_RUN=1" in profile_text
    assert "LIVE_CANARY_ENABLED=false" in profile_text
    assert "AUTORESEARCH_AUTO_LIVE_PROMOTE=false" in profile_text
    assert "RPC_URL" not in profile_text
    assert "PRIVATE_KEY" not in profile_text

    entries = load_scoreboard(tmp_path)
    assert len(entries) == 3
    assert any(entry["status"] == "accepted_replay" for entry in entries)

    for candidate_result in result.results:
        assert candidate_result.safety["ok"] is True
        assert candidate_result.replay["status"] == "completed"
        candidate_env = Path(candidate_result.sandbox["candidate_env_path"])
        assert candidate_env.exists()
        env_text = candidate_env.read_text(encoding="utf-8")
        assert "DRY_RUN=1" in env_text
        assert "LIVE_CANARY_ENABLED=false" in env_text

    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    assert report["live_remains_false"] is True
