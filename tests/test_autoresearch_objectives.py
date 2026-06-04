from __future__ import annotations

from research_loop.objectives import calculate_objective_score


def _baseline() -> dict:
    return {
        "total_pnl_usd": 10.0,
        "avg_pnl_pct": 5.0,
        "median_pnl_pct": 4.0,
        "win_rate_pct": 40.0,
        "runner_capture_ratio": 0.20,
        "moonshot_peak100_capture": 1.0,
        "moonshot_peak500_capture": 0.0,
        "moonshot_peak1000_capture": 0.0,
        "severe_loss_count": 0,
        "liquidity_crush_count": 0,
        "adverse_tick_count": 1,
        "no_pump_exit_count": 2,
        "max_drawdown_proxy": 5.0,
        "api_429_count": 0,
        "provider_degraded_minutes": 0,
        "overtrading_count": 0,
        "idle_no_buy_hours": 1,
    }


def test_improved_pnl_without_more_risk_is_accepted() -> None:
    candidate = _baseline()
    candidate.update(
        {
            "total_pnl_usd": 14.0,
            "avg_pnl_pct": 6.0,
            "median_pnl_pct": 5.0,
            "win_rate_pct": 45.0,
            "runner_capture_ratio": 0.25,
            "max_drawdown_proxy": 4.0,
        }
    )

    result = calculate_objective_score(_baseline(), candidate)

    assert result.accepted
    assert result.hard_gate_passed
    assert result.score > 0


def test_better_win_rate_with_more_severe_losses_is_rejected() -> None:
    candidate = _baseline()
    candidate.update(
        {
            "total_pnl_usd": 12.0,
            "win_rate_pct": 50.0,
            "severe_loss_count": 1,
        }
    )

    result = calculate_objective_score(_baseline(), candidate)

    assert not result.accepted
    assert not result.hard_gate_passed
    assert any("severe_loss_count" in reason for reason in result.rejection_reasons)


def test_moonshot_capture_with_api_budget_regression_is_rejected() -> None:
    candidate = _baseline()
    candidate.update(
        {
            "total_pnl_usd": 11.0,
            "moonshot_peak500_capture": 1.0,
            "api_429_count": 1,
        }
    )

    result = calculate_objective_score(_baseline(), candidate)

    assert not result.accepted
    assert not result.hard_gate_passed
    assert any("api_429_count" in reason for reason in result.rejection_reasons)


def test_median_pnl_drop_beyond_gate_is_rejected() -> None:
    candidate = _baseline()
    candidate.update(
        {
            "total_pnl_usd": 12.0,
            "avg_pnl_pct": 6.0,
            "median_pnl_pct": 0.0,
        }
    )

    result = calculate_objective_score(_baseline(), candidate)

    assert not result.accepted
    assert not result.hard_gate_passed
    assert any("median_pnl_pct" in reason for reason in result.rejection_reasons)
