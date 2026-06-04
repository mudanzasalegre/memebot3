from __future__ import annotations

from research_loop.evaluator import evaluate_replay_candidate


def _candidate() -> dict:
    return {
        "live_allowed": False,
        "changes": {"MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL": "75"},
    }


def _metrics() -> dict:
    return {
        "total_pnl_usd": 1.0,
        "avg_pnl_pct": 1.0,
        "median_pnl_pct": 1.0,
        "win_rate_pct": 50.0,
        "runner_capture_ratio": 0.1,
        "moonshot_peak100_capture": 0.0,
        "moonshot_peak500_capture": 0.0,
        "moonshot_peak1000_capture": 0.0,
        "severe_loss_count": 0,
        "liquidity_crush_count": 0,
        "adverse_tick_count": 0,
        "no_pump_exit_count": 0,
        "max_drawdown_proxy": 0.0,
        "provider_degraded_minutes": 0,
        "overtrading_count": 0,
        "idle_no_buy_hours": 0,
    }


def test_evaluator_rejects_api_budget_regression() -> None:
    baseline = _metrics()
    candidate = _metrics()
    candidate["total_pnl_usd"] = 2.0

    result = evaluate_replay_candidate(
        _candidate(),
        baseline,
        candidate,
        baseline_api_budget={"gecko_429_count": 0, "birdeye_429_count": 0, "jupiter_rate_limit_count": 0},
        candidate_api_budget={"gecko_429_count": 1, "birdeye_429_count": 0, "jupiter_rate_limit_count": 0},
    )

    assert result.status == "rejected"
    assert any("api_429_count_delta" in reason for reason in result.rejection_reasons)
