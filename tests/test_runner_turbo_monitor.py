from __future__ import annotations

from types import SimpleNamespace

from analytics import runner_turbo_monitor


def _cfg(**overrides: object) -> SimpleNamespace:
    values = {
        "RUNNER_TURBO_MONITOR_ENABLED": True,
        "RUNNER_TURBO_PEAK_PCT": 100.0,
        "RUNNER_TURBO_INTERVAL_S": 1.0,
        "RUNNER_TURBO_MAX_DURATION_MIN": 20.0,
        "RUNNER_TURBO_PAPER_ONLY": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_enters_turbo_at_peak_100() -> None:
    runner_turbo_monitor.reset_state()
    result = runner_turbo_monitor.observe_position("A", peak_pct=100, dry_run=True, cfg=_cfg())

    assert result["active"] is True
    assert result["reason"] == "entered"


def test_exits_turbo_on_close() -> None:
    runner_turbo_monitor.reset_state()
    runner_turbo_monitor.observe_position("A", peak_pct=100, dry_run=True, cfg=_cfg())

    result = runner_turbo_monitor.mark_closed("A")

    assert result["reason"] == "closed"


def test_normal_position_not_affected() -> None:
    runner_turbo_monitor.reset_state()
    result = runner_turbo_monitor.observe_position("A", peak_pct=50, dry_run=True, cfg=_cfg())

    assert result["active"] is False
    assert result["reason"] == "below_threshold"


def test_paper_only_does_not_activate_live() -> None:
    runner_turbo_monitor.reset_state()
    result = runner_turbo_monitor.observe_position("A", peak_pct=100, dry_run=False, cfg=_cfg())

    assert result["active"] is False
    assert result["reason"] == "disabled"
