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


def test_turbo_persists_enter_and_tick_events(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(runner_turbo_monitor, "_event_path", lambda root=None: tmp_path / "runner_turbo_events.jsonl")
    runner_turbo_monitor.reset_state(clear_persisted=True)

    runner_turbo_monitor.observe_position("A", peak_pct=100, dry_run=True, cfg=_cfg())
    runner_turbo_monitor.observe_position("A", peak_pct=120, dry_run=True, cfg=_cfg())
    report = runner_turbo_monitor.write_runner_turbo_monitor_report(tmp_path)

    assert (tmp_path / "runner_turbo_events.jsonl").exists()
    assert report["event_counts"][runner_turbo_monitor.EVENT_RUNNER_TURBO_ENTER] == 1
    assert report["event_counts"][runner_turbo_monitor.EVENT_RUNNER_TURBO_TICK] == 1


def test_exits_turbo_on_close() -> None:
    runner_turbo_monitor.reset_state()
    runner_turbo_monitor.observe_position("A", peak_pct=100, dry_run=True, cfg=_cfg())

    result = runner_turbo_monitor.mark_closed("A")

    assert result["reason"] == "closed"


def test_records_close_triggered_and_exit(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(runner_turbo_monitor, "_event_path", lambda root=None: tmp_path / "runner_turbo_events.jsonl")
    runner_turbo_monitor.reset_state(clear_persisted=True)
    runner_turbo_monitor.observe_position("A", peak_pct=100, dry_run=True, cfg=_cfg())

    triggered = runner_turbo_monitor.record_close_triggered("A", reason="DYNAMIC_RUNNER_FLOOR", peak_pct=100, pnl_pct=70)
    closed = runner_turbo_monitor.mark_closed("A")
    report = runner_turbo_monitor.write_runner_turbo_monitor_report(tmp_path)

    assert triggered["reason"] == "close_triggered"
    assert closed["reason"] == "closed"
    assert report["event_counts"][runner_turbo_monitor.EVENT_RUNNER_TURBO_CLOSE_TRIGGERED] == 1
    assert report["event_counts"][runner_turbo_monitor.EVENT_RUNNER_TURBO_EXIT] == 1


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
