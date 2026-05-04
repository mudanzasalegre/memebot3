from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import analytics.exit_policy as exit_policy
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


def test_research_rank_early_dump_cut_uses_lane_config() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = replace(
        original_cfg,
        RESEARCH_RANK_CANARY_EARLY_DUMP_PNL_PCT=-8.0,
        RESEARCH_RANK_CANARY_EARLY_DUMP_AFTER_S=25,
        RESEARCH_RANK_CANARY_EARLY_DUMP_CONFIRM_TICKS=1,
        RESEARCH_RANK_CANARY_EARLY_DUMP_IGNORE_IF_PEAK_PCT=10.0,
    )
    now = dt.datetime.now(dt.timezone.utc)
    subject = SimpleNamespace(
        entry_lane="pump_early_research_rank_canary",
        buy_price_usd=1.0,
        opened_at=now - dt.timedelta(seconds=30),
        highest_pnl_pct=0.0,
        early_dump_confirm_ticks=1,
    )
    try:
        assert exit_policy.should_exit(subject, 0.91, now) == "EARLY_DUMP_CUT"
    finally:
        exit_policy.CFG = original_cfg
