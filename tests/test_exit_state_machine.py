from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
from collections.abc import Iterator

import pytest

from analytics import exit_policy
from db.models import Position
from trader import seller


@contextlib.contextmanager
def patched_cfg(**overrides: object) -> Iterator[None]:
    original = exit_policy.CFG
    exit_policy.CFG = dataclasses.replace(original, **overrides)
    try:
        yield
    finally:
        exit_policy.CFG = original


def _subject(now: dt.datetime, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "entry_regime": "pump_early",
        "opened_at": now - dt.timedelta(minutes=5),
        "buy_price_usd": 1.0,
        "partial_taken": False,
        "partial_count": 0,
        "highest_pnl_pct": 0.0,
    }
    row.update(overrides)
    return row


def test_no_expansion_exits_when_position_never_expands() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = _subject(now, opened_at=now - dt.timedelta(minutes=4), highest_pnl_pct=1.0)

    with patched_cfg(
        EXIT_PROFILE_BY_REGIME=False,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=False,
        PRE_PARTIAL_RETRACE_TRIGGER_PCT=0.0,
        PRE_PARTIAL_TIME_STOP_MIN=0.0,
        PRE_PARTIAL_MAX_ADVERSE_PCT=0.0,
        NO_EXPANSION_WINDOW_MIN=3.0,
        NO_EXPANSION_MIN_PEAK_PCT=5.0,
        NO_EXPANSION_MAX_PCT=0.0,
        NO_PUMP_WINDOW_MIN=0.0,
        TIME_STOP_MIN=0.0,
    ):
        reason = exit_policy.should_exit(subject, price_now=1.0, now=now, pnl_pct=0.0)

    assert reason == "NO_EXPANSION"
    assert subject["exit_state"] == "pre_partial"


def test_pre_partial_retrace_exits_before_winner_turns_negative() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = _subject(now, highest_pnl_pct=8.0)

    with patched_cfg(
        EXIT_PROFILE_BY_REGIME=False,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=False,
        PRE_PARTIAL_RETRACE_TRIGGER_PCT=5.0,
        PRE_PARTIAL_RETRACE_GIVEBACK_PCT=6.0,
        PRE_PARTIAL_RETRACE_FLOOR_PCT=-1.5,
        PRE_PARTIAL_TIME_STOP_MIN=0.0,
        PRE_PARTIAL_MAX_ADVERSE_PCT=0.0,
        NO_EXPANSION_WINDOW_MIN=0.0,
        NO_PUMP_WINDOW_MIN=0.0,
        TIME_STOP_MIN=0.0,
    ):
        reason = exit_policy.should_exit(subject, price_now=1.01, now=now, pnl_pct=1.0)

    assert reason == "PRE_PARTIAL_RETRACE"


def test_partial_trigger_locks_out_full_take_profit() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = _subject(now, highest_pnl_pct=12.0)

    with patched_cfg(
        EXIT_PROFILE_BY_REGIME=False,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=False,
        TP_PARTIAL_ENABLED=True,
        TP_PARTIAL_TRIGGER_PCT=10.0,
        TP_PARTIAL_FRACTION=0.5,
        TAKE_PROFIT_PCT=10.0,
        PRE_PARTIAL_RETRACE_TRIGGER_PCT=0.0,
        PRE_PARTIAL_TIME_STOP_MIN=0.0,
        PRE_PARTIAL_MAX_ADVERSE_PCT=0.0,
        NO_EXPANSION_WINDOW_MIN=0.0,
        NO_PUMP_WINDOW_MIN=0.0,
        TIME_STOP_MIN=0.0,
    ):
        assert exit_policy.should_take_partial(subject, 12.0) is True
        reason = exit_policy.should_exit(subject, price_now=1.12, now=now, pnl_pct=12.0)

    assert reason is None


def test_max_adverse_excursion_bounds_no_partial_loss() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = _subject(now, opened_at=now - dt.timedelta(seconds=90), highest_pnl_pct=0.0)

    with patched_cfg(
        EXIT_PROFILE_BY_REGIME=False,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=False,
        STOP_LOSS_PCT=30.0,
        EARLY_DROP_KILL_PCT=0.0,
        PRE_PARTIAL_RETRACE_TRIGGER_PCT=0.0,
        PRE_PARTIAL_TIME_STOP_MIN=0.0,
        PRE_PARTIAL_MAX_ADVERSE_PCT=8.0,
        PRE_PARTIAL_MAX_ADVERSE_MIN_AGE_S=30.0,
        NO_EXPANSION_WINDOW_MIN=0.0,
        NO_PUMP_WINDOW_MIN=0.0,
        TIME_STOP_MIN=0.0,
    ):
        reason = exit_policy.should_exit(subject, price_now=0.91, now=now, pnl_pct=-9.0)

    assert reason == "MAX_ADVERSE_EXCURSION"
    assert subject["max_adverse_pnl_pct"] == pytest.approx(-9.0)


def test_partial_count_without_legacy_flag_keeps_positive_floor() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = _subject(
        now,
        partial_taken=False,
        partial_count=1,
        highest_pnl_pct=38.0,
        opened_at=now - dt.timedelta(minutes=10),
    )

    with patched_cfg(
        EXIT_PROFILE_BY_REGIME=False,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=False,
        POST_PARTIAL_PROTECTION_ENABLED=True,
        POST_PARTIAL_PROTECTION_PAPER_ENABLED=True,
        POST_PARTIAL_PROTECTION_EXECUTION_ENABLED=True,
        POST_PARTIAL_EXPERIMENT_SHADOW_ONLY=False,
        POST_PARTIAL_LOCK_FLOOR_ENABLED=True,
        POST_PARTIAL_LOCK_FLOOR_PCT=20.0,
        POST_PARTIAL_MAX_GIVEBACK_PCT=5.0,
        POST_PARTIAL_MIN_PEAK_PCT=35.0,
    ):
        reason = exit_policy.should_exit(subject, price_now=1.30, now=now, pnl_pct=30.0)

    assert reason == "POST_PARTIAL_TRAILING"
    assert subject["exit_state"] == "post_partial"


def test_seller_wrapper_updates_max_adverse_state() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    position = _subject(now, opened_at=now - dt.timedelta(seconds=90), highest_pnl_pct=0.0)

    with patched_cfg(
        EXIT_PROFILE_BY_REGIME=False,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=False,
        STOP_LOSS_PCT=30.0,
        EARLY_DROP_KILL_PCT=0.0,
        PRE_PARTIAL_RETRACE_TRIGGER_PCT=0.0,
        PRE_PARTIAL_TIME_STOP_MIN=0.0,
        PRE_PARTIAL_MAX_ADVERSE_PCT=8.0,
        PRE_PARTIAL_MAX_ADVERSE_MIN_AGE_S=30.0,
        NO_EXPANSION_WINDOW_MIN=0.0,
        NO_PUMP_WINDOW_MIN=0.0,
        TIME_STOP_MIN=0.0,
    ):
        reason = seller.check_exit_conditions(position, 0.90)

    assert reason == "MAX_ADVERSE_EXCURSION"
    assert position["max_adverse_pnl_pct"] == pytest.approx(-10.0)


def test_sqlalchemy_position_persists_exit_state_fields() -> None:
    position = Position(address="MINT", qty=1000, buy_price_usd=1.0, partial_count=0)

    exit_policy.update_exit_state(position, pnl_pct=-7.0)
    assert position.exit_state == "pre_partial"
    assert position.max_adverse_pnl_pct == pytest.approx(-7.0)

    position.partial_count = 1
    exit_policy.update_exit_state(position, pnl_pct=20.0)
    assert position.exit_state == "post_partial"
    assert position.highest_pnl_pct == pytest.approx(20.0)
