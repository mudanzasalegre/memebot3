from __future__ import annotations

import json

from research_loop.evaluator import evaluate_replay_candidate
from research_loop.scoreboard import (
    build_scoreboard_entry,
    load_scoreboard,
    record_run_evaluation,
    render_scoreboard_markdown,
    upsert_scoreboard_entry,
)


def _candidate() -> dict:
    return {
        "proposal_id": "ar_score_001",
        "created_at_utc": "2026-06-04T00:00:00+00:00",
        "live_allowed": False,
        "changes": {"MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL": "75"},
    }


def _baseline() -> dict:
    return {
        "total_pnl_usd": 10.0,
        "avg_pnl_pct": 2.0,
        "median_pnl_pct": 1.5,
        "win_rate_pct": 45.0,
        "closed_trades": 20,
        "runner_capture_ratio": 0.2,
        "moonshot_peak100_capture": 1.0,
        "moonshot_peak500_capture": 0.0,
        "moonshot_peak1000_capture": 0.0,
        "severe_loss_count": 0,
        "liquidity_crush_count": 0,
        "adverse_tick_count": 0,
        "no_pump_exit_count": 1,
        "max_drawdown_proxy": 0.0,
        "api_429_count": 0,
        "provider_degraded_minutes": 0,
        "overtrading_count": 0,
        "idle_no_buy_hours": 0,
    }


def _accepted_result():
    baseline = _baseline()
    candidate = {**baseline, "total_pnl_usd": 13.0, "median_pnl_pct": 2.0, "runner_capture_ratio": 0.25}
    return evaluate_replay_candidate(_candidate(), baseline, candidate)


def test_scoreboard_entry_contains_required_fields() -> None:
    entry = build_scoreboard_entry(
        run_id="ar_score_run",
        candidate_policy=_candidate(),
        evaluation_result=_accepted_result(),
        evaluated_at_utc="2026-06-04T01:00:00+00:00",
    )

    assert entry["run_id"] == "ar_score_run"
    assert entry["proposal_id"] == "ar_score_001"
    assert entry["status"] == "accepted_replay"
    assert entry["objective_score"] > 0
    assert entry["total_pnl_delta"] == 3.0
    assert entry["median_pnl_delta"] == 0.5
    assert entry["runner_capture_delta"] == 0.04999999999999999
    assert "api_429_count_delta" in entry["api_budget_delta"]


def test_scoreboard_writes_json_and_markdown(tmp_path) -> None:
    entry = build_scoreboard_entry(
        run_id="ar_score_run",
        candidate_policy=_candidate(),
        evaluation_result=_accepted_result(),
        evaluated_at_utc="2026-06-04T01:00:00+00:00",
    )

    upsert_scoreboard_entry(entry, root=tmp_path)

    assert len(load_scoreboard(tmp_path)) == 1
    assert (tmp_path / "data" / "research_runs" / "scoreboard.json").exists()
    markdown = (tmp_path / "data" / "research_runs" / "scoreboard.md").read_text(encoding="utf-8")
    assert "AutoResearch Scoreboard" in markdown
    assert "ar_score_run" in markdown


def test_scoreboard_upsert_replaces_same_run_and_proposal(tmp_path) -> None:
    entry = build_scoreboard_entry(
        run_id="ar_score_run",
        candidate_policy=_candidate(),
        evaluation_result=_accepted_result(),
    )
    upsert_scoreboard_entry(entry, root=tmp_path)
    changed = {**entry, "status": "rejected", "objective_score": -1.0}
    upsert_scoreboard_entry(changed, root=tmp_path)

    entries = load_scoreboard(tmp_path)
    assert len(entries) == 1
    assert entries[0]["status"] == "rejected"
    assert entries[0]["objective_score"] == -1.0


def test_record_run_evaluation_updates_scoreboard(tmp_path) -> None:
    run_dir = tmp_path / "data" / "research_runs" / "runs" / "ar_score_record"
    run_dir.mkdir(parents=True)
    (run_dir / "candidate_policy.json").write_text(json.dumps(_candidate()), encoding="utf-8")
    (run_dir / "replay_metrics.json").write_text(
        json.dumps({**_baseline(), "total_pnl_usd": 13.0, "median_pnl_pct": 2.0, "runner_capture_ratio": 0.25}),
        encoding="utf-8",
    )
    baseline_path = run_dir / "baseline_metrics.json"
    baseline_path.write_text(json.dumps(_baseline()), encoding="utf-8")

    entry = record_run_evaluation(run_dir, root=tmp_path)

    assert entry["run_id"] == "ar_score_record"
    assert entry["status"] == "accepted_replay"
    assert len(load_scoreboard(tmp_path)) == 1


def test_render_scoreboard_markdown_handles_empty_entries() -> None:
    markdown = render_scoreboard_markdown([])

    assert "AutoResearch Scoreboard" in markdown
    assert "| Run | Proposal | Status |" in markdown
