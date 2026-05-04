from __future__ import annotations

from dataclasses import replace

import analytics.exit_policy as exit_policy


def _subject(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "entry_regime": "pump_early",
        "opened_at": "2026-04-10T18:00:00+00:00",
        "buy_price_usd": 1.0,
        "highest_pnl_pct": 38.0,
        "partial_taken": True,
    }
    payload.update(overrides)
    return payload


def _with_pump_protection() -> object:
    return replace(
        exit_policy.CFG,
        EXIT_PROFILE_BY_REGIME=True,
        POST_PARTIAL_PROTECTION_ENABLED=False,
        POST_PARTIAL_PROTECTION_PAPER_ENABLED=True,
        POST_PARTIAL_PROTECTION_LIVE_ENABLED=False,
        POST_PARTIAL_LOCK_FLOOR_ENABLED=True,
        POST_PARTIAL_MIN_PEAK_PCT=35.0,
        POST_PARTIAL_LOCK_FLOOR_PCT=0.0,
        POST_PARTIAL_MAX_GIVEBACK_PCT=0.0,
        PUMP_EARLY_POST_PARTIAL_PROTECTION_ENABLED=True,
        PUMP_EARLY_POST_PARTIAL_LOCK_FLOOR_PCT=20.0,
        PUMP_EARLY_POST_PARTIAL_MAX_GIVEBACK_PCT=5.0,
        PUMP_EARLY_POST_PARTIAL_STOP_PCT=2.0,
        PUMP_EARLY_POST_PARTIAL_TRAILING_PCT=3.0,
    )


def test_post_partial_protection_replaces_legacy_trailing_after_lock_floor() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = _with_pump_protection()
    try:
        reason = exit_policy.should_exit(
            _subject(highest_pnl_pct=38.0),
            price_now=1.345,
            now=exit_policy.dt.datetime(2026, 4, 10, 18, 5, tzinfo=exit_policy.dt.timezone.utc),
            pnl_pct=34.5,
        )
    finally:
        exit_policy.CFG = original_cfg

    assert reason is None


def test_post_partial_protection_triggers_giveback_cap_once_armed() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = _with_pump_protection()
    try:
        reason = exit_policy.should_exit(
            _subject(highest_pnl_pct=38.0),
            price_now=1.329,
            now=exit_policy.dt.datetime(2026, 4, 10, 18, 5, tzinfo=exit_policy.dt.timezone.utc),
            pnl_pct=32.9,
        )
    finally:
        exit_policy.CFG = original_cfg

    assert reason == "POST_PARTIAL_TRAILING"


def test_post_partial_protection_uses_lock_floor_when_peak_just_above_arm() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = _with_pump_protection()
    try:
        reason = exit_policy.should_exit(
            _subject(highest_pnl_pct=36.0),
            price_now=1.199,
            now=exit_policy.dt.datetime(2026, 4, 10, 18, 5, tzinfo=exit_policy.dt.timezone.utc),
            pnl_pct=19.9,
        )
    finally:
        exit_policy.CFG = original_cfg

    assert reason == "POST_PARTIAL_TRAILING"


def test_post_partial_protection_live_disabled_by_default() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = replace(
        exit_policy.CFG,
        DRY_RUN=False,
        POST_PARTIAL_PROTECTION_ENABLED=True,
        POST_PARTIAL_PROTECTION_PAPER_ENABLED=True,
        POST_PARTIAL_PROTECTION_LIVE_ENABLED=False,
        PUMP_EARLY_POST_PARTIAL_PROTECTION_ENABLED=None,
        POST_PARTIAL_LOCK_FLOOR_PCT=20.0,
        POST_PARTIAL_MAX_GIVEBACK_PCT=5.0,
        POST_PARTIAL_MIN_PEAK_PCT=35.0,
    )
    try:
        policy = exit_policy.effective_exit_policy(_subject(highest_pnl_pct=50.0))
    finally:
        exit_policy.CFG = original_cfg

    assert policy.post_partial_protection_enabled is False
