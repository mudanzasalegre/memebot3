from __future__ import annotations

import json

from research_loop.evaluator import evaluate_replay_candidate, evaluate_replay_run


def _candidate() -> dict:
    return {
        "proposal_id": "ar_eval_001",
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


def test_candidate_better_is_accepted_replay() -> None:
    baseline = _baseline()
    candidate = {**baseline, "total_pnl_usd": 13.0, "median_pnl_pct": 2.0, "runner_capture_ratio": 0.25}

    result = evaluate_replay_candidate(_candidate(), baseline, candidate)

    assert result.status == "accepted_replay"
    assert result.accepted
    assert result.objective is not None
    assert result.objective.score > 0


def test_candidate_with_small_sample_needs_paper() -> None:
    baseline = _baseline()
    candidate = {**baseline, "total_pnl_usd": 13.0, "median_pnl_pct": 2.0, "runner_capture_ratio": 0.25, "closed_trades": 2}

    result = evaluate_replay_candidate(_candidate(), baseline, candidate, min_closed_trades=5)

    assert result.status == "needs_paper"
    assert result.needs_paper
    assert not result.accepted


def test_candidate_with_worse_severe_losses_is_rejected() -> None:
    baseline = _baseline()
    candidate = {**baseline, "total_pnl_usd": 14.0, "severe_loss_count": 1}

    result = evaluate_replay_candidate(_candidate(), baseline, candidate)

    assert result.status == "rejected"
    assert any("severe_loss_count" in reason for reason in result.rejection_reasons)


def test_candidate_with_api_budget_worse_is_rejected() -> None:
    baseline = _baseline()
    candidate = {**baseline, "total_pnl_usd": 14.0}

    result = evaluate_replay_candidate(
        _candidate(),
        baseline,
        candidate,
        baseline_api_budget={"gecko_429_count": 0, "birdeye_429_count": 0, "jupiter_rate_limit_count": 0},
        candidate_api_budget={"gecko_429_count": 0, "birdeye_429_count": 0, "jupiter_rate_limit_count": 1},
    )

    assert result.status == "rejected"
    assert any("api_429_count_delta" in reason for reason in result.rejection_reasons)


def test_candidate_crash_is_failed() -> None:
    result = evaluate_replay_candidate(_candidate(), _baseline(), {"failed": True})

    assert result.status == "failed"
    assert result.rejection_reasons == ["replay_failed"]


def test_missing_comparable_metrics_is_inconclusive() -> None:
    result = evaluate_replay_candidate(_candidate(), {"total_pnl_usd": 1.0}, {"total_pnl_usd": 2.0})

    assert result.status == "inconclusive"
    assert "missing_comparable_metrics" in result.rejection_reasons


def test_evaluate_replay_run_reads_files(tmp_path) -> None:
    run_dir = tmp_path / "data" / "research_runs" / "runs" / "ar_eval_run"
    run_dir.mkdir(parents=True)
    (run_dir / "candidate_policy.json").write_text(json.dumps(_candidate()), encoding="utf-8")
    (run_dir / "replay_metrics.json").write_text(
        json.dumps({**_baseline(), "total_pnl_usd": 13.0, "median_pnl_pct": 2.0, "runner_capture_ratio": 0.25}),
        encoding="utf-8",
    )
    baseline_path = run_dir / "baseline_metrics.json"
    baseline_path.write_text(json.dumps(_baseline()), encoding="utf-8")

    result = evaluate_replay_run(run_dir)

    assert result.status == "accepted_replay"
    assert result.run_id == "ar_eval_run"
    assert result.proposal_id == "ar_eval_001"
