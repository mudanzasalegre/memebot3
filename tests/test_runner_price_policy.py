from __future__ import annotations

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from analytics import exit_policy, runner_price_policy


def _cfg(**overrides):
    return SimpleNamespace(**{
        "RUNNER_PRICE_TRAILING_PAPER_ENABLED": True,
        "RUNNER_PRICE_TRAILING_MIN_PEAK_PCT": 300.0,
        "RUNNER_PRICE_TRAILING_DRAWDOWN_PCT": 20.0,
        "RUNNER_PRICE_TRAILING_MAX_HOLD_H": 24.0,
        **overrides,
    })


def _subject(peak, *, lane="pump_early_moonshot_micro_lottery", **overrides):
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "entry_regime": "pump_early", "entry_lane": lane,
        "opened_at": now - dt.timedelta(minutes=5), "buy_price_usd": 1.0,
        "entry_qty": 1000, "qty": 250, "realized_qty": 750,
        "realized_proceeds_usd": 3000.0, "partial_taken": True,
        "partial_count": 3, "highest_pnl_pct": peak, "dry_run": True,
        "runner_trailing_policy": runner_price_policy.freeze_policy(_cfg(), dry_run=True),
        **overrides,
    }


@pytest.mark.parametrize("peak, floor", [(300, 220), (500, 380), (1000, 780),
                                         (5000, 3980), (10000, 7980), (100000, 79980)])
def test_drawdown_is_measured_in_peak_price_not_profit_points(peak, floor):
    assert runner_price_policy.price_drawdown_floor_pct(peak, 20) == pytest.approx(floor)


@pytest.mark.parametrize("peak", [500, 1000, 5000, 10000, 100000])
@pytest.mark.parametrize("lane", ["pump_early_moonshot_micro_lottery", "pump_early_green_sniper", "normal"])
def test_extreme_runner_survives_small_price_retrace_but_exits_at_drawdown(peak, lane):
    now = dt.datetime.now(dt.timezone.utc)
    floor = runner_price_policy.price_drawdown_floor_pct(peak, 20)
    # 5% of peak price, NOT five percentage points of entry-price profit.
    current = (100 + peak) * .95 - 100
    subject = _subject(peak, lane=lane)
    assert exit_policy.should_exit(dict(subject), 1 + current / 100, now, pnl_pct=current) is None
    assert exit_policy.runner_giveback_emergency_reason(subject, pnl_pct=current, peak=peak) is None
    assert exit_policy.should_exit(dict(subject), 1 + floor / 100, now, pnl_pct=floor) == "DYNAMIC_RUNNER_FLOOR"
    assert exit_policy.should_exit(dict(subject), 1 + (peak + 100) / 100, now, pnl_pct=peak + 100) is None


@pytest.mark.parametrize("overrides", [{"dry_run": False}, {"dry_run": "false"}, {"dry_run": 2},
                                      {"runner_trailing_policy": None}, {"runner_trailing_policy": "broken"},
                                      {"partial_taken": False, "partial_count": 0, "realized_qty": 0}])
def test_no_new_policy_for_live_legacy_corrupt_or_pre_partial_positions(overrides):
    assert exit_policy.runner_price_protection_floor_pct(_subject(5000, **overrides), peak=5000) is None


def test_frozen_policy_does_not_follow_global_config_mutation(monkeypatch):
    subject = _subject(5000)
    monkeypatch.setattr(exit_policy, "CFG", _cfg(RUNNER_PRICE_TRAILING_DRAWDOWN_PCT=40))
    assert exit_policy.runner_price_protection_floor_pct(subject, peak=5000) == pytest.approx(3980)


@pytest.mark.parametrize("setting, bad", [
    ("RUNNER_PRICE_TRAILING_DRAWDOWN_PCT", float("nan")),
    ("RUNNER_PRICE_TRAILING_DRAWDOWN_PCT", 0), ("RUNNER_PRICE_TRAILING_DRAWDOWN_PCT", 90),
    ("RUNNER_PRICE_TRAILING_MIN_PEAK_PCT", float("inf")),
    ("RUNNER_PRICE_TRAILING_MAX_HOLD_H", 0), ("RUNNER_PRICE_TRAILING_MAX_HOLD_H", 999),
])
def test_invalid_settings_disable_without_crashing_buy(setting, bad):
    frozen = runner_price_policy.freeze_policy(_cfg(**{setting: bad}), dry_run=True)
    assert json.loads(frozen)["enabled"] is False
    assert runner_price_policy.parse_policy(frozen) is None


def test_live_snapshot_cannot_enable_paper_policy():
    assert json.loads(runner_price_policy.freeze_policy(_cfg(), dry_run=False))["enabled"] is False


@pytest.mark.parametrize("liquidity", [0, 1])
def test_runner_holding_extension_is_bounded_and_does_not_ignore_liquidity_crush(liquidity):
    now = dt.datetime.now(dt.timezone.utc)
    subject = _subject(5000, opened_at=now - dt.timedelta(hours=8), buy_liquidity_usd=10000)
    assert exit_policy.should_exit(dict(subject), 50.0, now, pnl_pct=4900) is None
    assert exit_policy.should_exit(dict(subject), 50.0, now, pnl_pct=4900, liq_now=liquidity) == "LIQUIDITY_CRUSH"
    subject["opened_at"] = now - dt.timedelta(hours=24)
    assert exit_policy.should_exit(subject, 50.0, now, pnl_pct=4900) == "TIMEOUT_RUNNER"


@pytest.mark.parametrize("liquidity", [None, True, -1, float("nan"), float("inf")])
def test_extreme_runner_does_not_invent_liquidity_collapse_from_unknown_values(liquidity):
    now = dt.datetime.now(dt.timezone.utc)
    subject = _subject(5000, opened_at=now - dt.timedelta(hours=8), buy_liquidity_usd=10000)
    assert exit_policy.should_exit(subject, 50.0, now, pnl_pct=4900, liq_now=liquidity) is None


def test_missing_price_does_not_extend_runner_timeout():
    now = dt.datetime.now(dt.timezone.utc)
    subject = _subject(5000, opened_at=now - dt.timedelta(hours=25))
    assert exit_policy.should_exit(subject, None, now) == "TIMEOUT_NOPRICE"
    assert exit_policy.should_exit(subject, float("nan"), now) == "TIMEOUT_NOPRICE"


def test_schema_upgrade_is_idempotent_and_preserves_existing_positions(tmp_path, monkeypatch):
    import db.database as database
    from db.models import Position

    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
        monkeypatch.setattr(database, "engine", engine)
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql("CREATE TABLE positions (id INTEGER PRIMARY KEY, address TEXT)")
                await conn.exec_driver_sql("INSERT INTO positions VALUES (1, 'CaseSensitiveMint')")
            await database._ensure_position_columns()
            await database._ensure_position_columns()
            async with engine.begin() as conn:
                record = (await conn.exec_driver_sql("SELECT address, runner_trailing_policy FROM positions")).one()
            assert record == ("CaseSensitiveMint", None)
            assert "runner_trailing_policy" in Position.__table__.columns
        finally:
            await engine.dispose()
    asyncio.run(check())
