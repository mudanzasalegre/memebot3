from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from analytics.exit_policy import should_exit


def test_green_sniper_early_dump_cut_fires() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = SimpleNamespace(
        entry_lane="pump_early_green_candle_sniper",
        buy_price_usd=1.0,
        opened_at=now - dt.timedelta(seconds=45),
        highest_pnl_pct=0.0,
        early_dump_confirm_ticks=2,
    )
    assert should_exit(subject, 0.87, now) == "EARLY_DUMP_CUT"


def test_green_sniper_early_dump_ignores_prior_peak() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = SimpleNamespace(
        entry_lane="pump_early_green_candle_sniper",
        buy_price_usd=1.0,
        opened_at=now - dt.timedelta(seconds=45),
        highest_pnl_pct=20.0,
        early_dump_confirm_ticks=2,
    )
    assert should_exit(subject, 0.87, now) != "EARLY_DUMP_CUT"
