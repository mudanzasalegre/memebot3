from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import pytest

import analytics.exit_policy as exit_policy


def _cfg() -> object:
    return replace(
        exit_policy.CFG,
        DRY_RUN=True,
        TP_PARTIAL_ENABLED=True,
        POST_PARTIAL_PROTECTION_ENABLED=True,
        POST_PARTIAL_PROTECTION_PAPER_ENABLED=True,
        POST_PARTIAL_PROTECTION_LIVE_ENABLED=False,
        POST_PARTIAL_PROTECTION_EXECUTION_ENABLED=True,
        POST_PARTIAL_EXPERIMENT_SHADOW_ONLY=False,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=True,
        BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED=True,
        BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED=False,
        BIRD_TP1_PCT=25.0,
        BIRD_TP1_FRACTION=0.25,
        BIRD_TP2_PCT=50.0,
        BIRD_TP2_FRACTION=0.25,
        BIRD_TP3_PCT=100.0,
        BIRD_TP3_FRACTION=0.20,
        BIRD_TP4_PCT=300.0,
        BIRD_TP4_FRACTION=0.15,
        BIRD_MOONBAG_FRACTION=0.15,
        RUNNER_GIVEBACK_EMERGENCY_ENABLED=True,
        RUNNER_GIVEBACK_EMERGENCY_PAPER_ENABLED=True,
        RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED=False,
        RUNNER_GIVEBACK_PEAK_100_MAX_GIVEBACK=25.0,
        RUNNER_GIVEBACK_PEAK_300_MAX_GIVEBACK=60.0,
        RUNNER_GIVEBACK_PEAK_700_MAX_GIVEBACK=120.0,
        RUNNER_GIVEBACK_PEAK_1000_MAX_GIVEBACK=220.0,
        RUNNER_GIVEBACK_PEAK_2000_MAX_GIVEBACK=450.0,
        RUNNER_GIVEBACK_CLOSE_REMAINING=True,
    )


def _subject(**overrides: object) -> SimpleNamespace:
    base = {
        "entry_regime": "pump_early",
        "dry_run": True,
        "opened_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
        "buy_price_usd": 1.0,
        "highest_pnl_pct": 0.0,
        "partial_taken": False,
        "entry_qty": 1_000,
        "qty": 1_000,
        "realized_qty": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_multi_partial_first_step_sells_25_pct_of_position() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = _cfg()
    try:
        subject = _subject(highest_pnl_pct=25.0)
        assert exit_policy.should_take_partial(subject, 25.0) is True
        assert exit_policy.partial_sell_fraction(subject, 25.0) == pytest.approx(0.25)
    finally:
        exit_policy.CFG = original_cfg


def test_multi_partial_catches_up_when_peak_jumps_to_100() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = _cfg()
    try:
        subject = _subject(partial_taken=True, highest_pnl_pct=100.0, entry_qty=1_000, qty=750, realized_qty=250)
        assert exit_policy.partial_sell_fraction(subject, 100.0) == pytest.approx(0.60)
    finally:
        exit_policy.CFG = original_cfg


def test_multi_partial_at_300_secures_all_ladder_except_moonbag() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = _cfg()
    try:
        subject = _subject(highest_pnl_pct=300.0)
        plan = exit_policy.partial_ladder_plan(subject, 300.0)
        assert plan["target_secured_fraction"] == pytest.approx(0.85)
        assert exit_policy.partial_sell_fraction(subject, 300.0) == pytest.approx(0.85)
    finally:
        exit_policy.CFG = original_cfg


def test_emergency_giveback_closes_remaining_after_large_runner_retrace() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = _cfg()
    try:
        now = dt.datetime.now(dt.timezone.utc)
        subject = _subject(partial_taken=True, highest_pnl_pct=1000.0, qty=150, realized_qty=850)
        reason = exit_policy.should_exit(subject, price_now=8.5, now=now, pnl_pct=750.0)
        assert reason == "RUNNER_GIVEBACK_EMERGENCY"
        assert exit_policy.runner_giveback_emergency_reason(subject, pnl_pct=850.0, peak=1000.0) is None
    finally:
        exit_policy.CFG = original_cfg


def test_multi_partial_does_not_duplicate_filled_steps() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = _cfg()
    try:
        subject = _subject(partial_taken=True, highest_pnl_pct=50.0, entry_qty=1_000, qty=500, realized_qty=500)
        assert exit_policy.partial_sell_fraction(subject, 50.0) == 0.0
        assert exit_policy.should_take_partial(subject, 50.0) is False
    finally:
        exit_policy.CFG = original_cfg


def test_multi_partial_is_not_live_enabled_by_default() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = replace(_cfg(), DRY_RUN=False)
    try:
        subject = _subject(dry_run=False, partial_taken=True, highest_pnl_pct=300.0)
        plan = exit_policy.partial_ladder_plan(subject, 300.0)
        assert plan["enabled"] is False
        assert exit_policy.runner_giveback_emergency_reason(subject, pnl_pct=850.0, peak=1000.0) is None
    finally:
        exit_policy.CFG = original_cfg


def test_jackpot_runner_cannot_suppress_global_bird_tp1_in_paper() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = replace(
        _cfg(),
        PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP1_PCT=100.0,
        PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP1_FRACTION=0.20,
    )
    try:
        subject = _subject(
            entry_lane="pump_early_research_rank_canary",
            gate_profile="research_rank_canary",
            buy_dex_id="pumpswap",
            buy_liquidity_is_proxy=0,
            buy_liquidity_usd=22_000.0,
            buy_market_cap_usd=77_000.0,
            buy_price_pct_5m=76.0,
            buy_txns_last_5m=1700.0,
            research_rank_score=75.0,
            highest_pnl_pct=40.2,
        )
        plan = exit_policy.partial_ladder_plan(subject, 40.2)
        assert plan["triggered_steps"][0]["trigger_pct"] == pytest.approx(25.0)
        assert plan["sell_fraction_of_remaining"] == pytest.approx(0.25)
    finally:
        exit_policy.CFG = original_cfg


def test_moonshot_lottery_uses_own_ladder_not_bird_tp1() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = _cfg()
    try:
        subject = _subject(entry_lane="pump_early_moonshot_micro_lottery", gate_profile="moonshot_micro_lottery")
        assert exit_policy.should_take_partial(subject, 40.0) is False
        assert exit_policy.should_take_partial(subject, 50.0) is True
        assert exit_policy.partial_sell_fraction(subject, 50.0) == pytest.approx(0.40)
    finally:
        exit_policy.CFG = original_cfg
