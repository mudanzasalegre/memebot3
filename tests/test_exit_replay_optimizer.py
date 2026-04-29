from __future__ import annotations

from backtest.exit_replay_optimizer import simulate_exit_profile


def test_post_partial_protected_improves_runner_capture() -> None:
    row = {"pnl_pct": 20, "max_pnl_pct_seen": 100}
    assert simulate_exit_profile(row, "post_partial_protected") > simulate_exit_profile(row, "current")
