from __future__ import annotations

from backtest.exit_simulator import compare_exit_profiles


def test_exit_simulator_compares_profiles() -> None:
    report = compare_exit_profiles([
        {"total_pnl_pct": 50, "max_pnl_pct_seen": 120},
        {"total_pnl_pct": -10, "max_pnl_pct_seen": 0},
    ])
    assert "runner_18pct_35" in report
    assert report["runner_18pct_35"]["trades_over_100"] >= 0
