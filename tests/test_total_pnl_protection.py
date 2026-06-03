from __future__ import annotations

import datetime as dt

import pytest

from analytics.exit_policy import should_exit, total_pnl_protection_floor_pct
from trade_pnl import total_pnl_pct_from_record


def test_total_pnl_floor_uses_realized_and_unrealized() -> None:
    subject = {
        "entry_regime": "pump_early",
        "opened_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
        "buy_price_usd": 1.0,
        "entry_qty": 1000,
        "qty": 500,
        "realized_qty": 500,
        "realized_proceeds_usd": 750.0,
        "partial_taken": True,
        "partial_count": 2,
        "highest_pnl_pct": 80.0,
    }

    assert total_pnl_pct_from_record(subject, close_price_usd=0.2) == pytest.approx(-15.0)
    assert total_pnl_protection_floor_pct(subject, peak=80.0) == 15.0
    assert should_exit(subject, price_now=0.2, now=dt.datetime.now(dt.timezone.utc), pnl_pct=-80.0) == "TOTAL_PNL_PROTECTION_EXIT"


def test_peak_300_uses_total_floor_150() -> None:
    subject = {
        "entry_regime": "pump_early",
        "opened_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
        "buy_price_usd": 1.0,
        "entry_qty": 1000,
        "qty": 500,
        "realized_qty": 500,
        "realized_proceeds_usd": 1000.0,
        "partial_taken": True,
        "partial_count": 1,
        "highest_pnl_pct": 300.0,
    }

    assert total_pnl_protection_floor_pct(subject, peak=300.0) == 150.0
    assert should_exit(subject, price_now=2.0, now=dt.datetime.now(dt.timezone.utc), pnl_pct=100.0) == "TOTAL_PNL_PROTECTION_EXIT"
