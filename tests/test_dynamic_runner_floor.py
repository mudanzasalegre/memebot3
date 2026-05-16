from __future__ import annotations

import datetime as dt

import analytics.exit_policy as exit_policy


def _subject(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "entry_regime": "pump_early",
        "opened_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
        "buy_price_usd": 1.0,
        "partial_taken": True,
        "highest_pnl_pct": 300.0,
    }
    base.update(overrides)
    return base


def test_peak_300_closes_around_floor() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    reason = exit_policy.should_exit(_subject(highest_pnl_pct=300.0), price_now=2.0, now=now, pnl_pct=100.0)

    assert reason == "DYNAMIC_RUNNER_FLOOR"


def test_peak_1000_does_not_fall_to_100() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    reason = exit_policy.should_exit(_subject(highest_pnl_pct=1000.0), price_now=2.0, now=now, pnl_pct=100.0)

    assert reason == "DYNAMIC_RUNNER_FLOOR"
    assert exit_policy.dynamic_runner_floor_pct(_subject(highest_pnl_pct=1000.0), peak=1000.0) == 500.0


def test_floor_does_not_apply_before_runner() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    reason = exit_policy.should_exit(_subject(highest_pnl_pct=50.0), price_now=1.2, now=now, pnl_pct=20.0)

    assert reason != "DYNAMIC_RUNNER_FLOOR"
