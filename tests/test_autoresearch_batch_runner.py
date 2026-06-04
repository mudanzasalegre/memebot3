from __future__ import annotations

import json
from pathlib import Path

from research_loop.batch_runner import run_research_batch
from research_loop.replay_runner import ReplayRunResult
from research_loop.scoreboard import load_scoreboard


def _candidate(proposal_id: str, changes: dict | None = None) -> dict:
    return {
        "proposal_id": proposal_id,
        "created_at_utc": "2026-06-04T00:00:00+00:00",
        "experiment_type": "replay",
        "hypothesis": "Batch test candidate",
        "target_lanes": ["pump_early_moonshot_micro_lottery"],
        "changes": changes or {"MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL": "75"},
        "expected_effect": {"increase_pnl": True, "increase_moonshot_capture": True},
        "required_gates": ["replay_positive", "api_budget_ok"],
        "api_budget_sensitive": True,
        "live_allowed": False,
        "risk_notes": ["paper only"],
    }


def _baseline() -> dict:
    return {
        "total_pnl_usd": 10.0,
        "avg_pnl_pct": 2.0,
        "median_pnl_pct": 1.0,
        "win_rate_pct": 40.0,
        "closed_trades": 10,
        "runner_capture_ratio": 0.2,
        "moonshot_peak100_capture": 0.0,
        "moonshot_peak500_capture": 0.0,
        "moonshot_peak1000_capture": 0.0,
        "severe_loss_count": 0,
        "liquidity_crush_count": 0,
        "adverse_tick_count": 0,
        "no_pump_exit_count": 0,
        "max_drawdown_proxy": 0.0,
        "api_429_count": 0,
        "provider_degraded_minutes": 0,
        "overtrading_count": 0,
        "idle_no_buy_hours": 0,
    }


def _replay_metrics(total_pnl: float = 13.0) -> dict:
    metrics = _baseline()
    metrics.update(
        {
            "total_pnl_usd": total_pnl,
            "median_pnl_pct": 1.5,
            "runner_capture_ratio": 0.25,
        }
    )
    return metrics


def _fake_replay(total_pnl: float = 13.0):
    def replay(sandbox):
        path = sandbox.run_dir / "replay_metrics.json"
        path.write_text(json.dumps(_replay_metrics(total_pnl)), encoding="utf-8")
        return ReplayRunResult(
            run_id=sandbox.run_id,
            run_dir=sandbox.run_dir,
            status="completed",
            replay_metrics_path=path,
            report_snapshot_dir=sandbox.run_dir / "report_snapshot",
            replay_metrics=_replay_metrics(total_pnl),
            warnings=[],
            failures=[],
        )

    return replay


def test_batch_runner_generates_replays_evaluates_scoreboard_and_checkpoint(tmp_path) -> None:
    result = run_research_batch(
        space_name="moonshot_micro",
        n=1,
        seed=42,
        root=tmp_path,
        batch_id="batch_test",
        candidates=[_candidate("ar_batch_001")],
        replay_func=_fake_replay(),
        baseline_metrics=_baseline(),
    )

    assert result.completed == 1
    assert result.failed == 0
    assert result.skipped == 0
    assert result.results[0].status == "accepted_replay"
    assert result.checkpoint_path.exists()
    assert len(load_scoreboard(tmp_path)) == 1
    assert (result.batch_dir / "batch_result.json").exists()


def test_batch_runner_skips_duplicate_changes_by_checkpoint(tmp_path) -> None:
    result = run_research_batch(
        space_name="moonshot_micro",
        n=2,
        seed=42,
        root=tmp_path,
        batch_id="batch_dupes",
        candidates=[
            _candidate("ar_batch_001", {"MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL": "75"}),
            _candidate("ar_batch_002", {"MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL": "75"}),
        ],
        replay_func=_fake_replay(),
        baseline_metrics=_baseline(),
    )

    assert result.completed == 1
    assert result.skipped == 1
    assert result.results[1].status == "skipped_duplicate"
    assert "changes_hash" in result.results[1].duplicate_reasons


def test_batch_runner_continues_when_candidate_fails(tmp_path) -> None:
    def replay(sandbox):
        if sandbox.run_id == "ar_batch_fail":
            raise RuntimeError("boom")
        return _fake_replay()(sandbox)

    result = run_research_batch(
        space_name="moonshot_micro",
        n=2,
        seed=42,
        root=tmp_path,
        batch_id="batch_continue",
        candidates=[
            _candidate("ar_batch_fail", {"MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL": "75"}),
            _candidate("ar_batch_ok", {"MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL": "100"}),
        ],
        replay_func=replay,
        baseline_metrics=_baseline(),
    )

    assert result.failed == 1
    assert result.completed == 1
    assert result.results[0].status == "failed"
    assert result.results[1].status == "accepted_replay"


def test_batch_runner_auto_space_uses_bandit(tmp_path) -> None:
    result = run_research_batch(
        space_name="auto",
        n=1,
        seed=42,
        root=tmp_path,
        batch_id="batch_auto",
        candidates=[_candidate("ar_batch_auto", {"MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL": "75"})],
        replay_func=_fake_replay(),
        baseline_metrics=_baseline(),
    )

    assert result.space in {"rank_canary", "shadow_followup", "moonshot_micro", "runner_exit", "sniper_momentum", "paper_exploration", "late_momentum", "lane_sizing"}
