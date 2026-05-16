from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_MODULE_PATH = _ROOT / "analytics" / "exit_policy.py"
_SPEC = importlib.util.spec_from_file_location("exit_policy_under_test", _MODULE_PATH)
assert _SPEC and _SPEC.loader
exit_policy = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = exit_policy
_SPEC.loader.exec_module(exit_policy)


def test_pump_early_partial_trigger_uses_regime_override() -> None:
    subject = {"entry_regime": "pump_early", "partial_taken": False}
    policy = exit_policy.effective_exit_policy(subject)

    assert exit_policy.should_take_partial(subject, float(policy.tp_partial_trigger_pct)) is True
    assert exit_policy.should_take_partial(subject, float(policy.tp_partial_trigger_pct) - 0.1) is False


def test_pump_early_pre_partial_time_stop_triggers() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = {
        "entry_regime": "pump_early",
        "opened_at": now - dt.timedelta(minutes=3, seconds=30),
        "buy_price_usd": 1.0,
        "highest_pnl_pct": 1.0,
        "partial_taken": False,
    }

    reason = exit_policy.should_exit(subject, price_now=0.97, now=now, pnl_pct=-3.0)
    assert reason == "PRE_PARTIAL_TIME_STOP"


def test_pump_early_pre_partial_retrace_triggers() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = {
        "entry_regime": "pump_early",
        "opened_at": now - dt.timedelta(minutes=2),
        "buy_price_usd": 1.0,
        "highest_pnl_pct": 7.0,
        "partial_taken": False,
    }

    reason = exit_policy.should_exit(subject, price_now=1.0, now=now, pnl_pct=0.0)
    assert reason == "PRE_PARTIAL_RETRACE"


def test_pumpswap_profit_adverse_tick_precedes_stop_loss() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_pumpswap_profit",
        "gate_profile": "pumpswap_profit_broad",
        "opened_at": now - dt.timedelta(seconds=80),
        "buy_price_usd": 1.0,
        "highest_pnl_pct": 0.0,
        "partial_taken": False,
    }

    reason = exit_policy.should_exit(subject, price_now=0.91, now=now, pnl_pct=-9.0)
    assert reason == "ADVERSE_TICK"


def test_pumpswap_profit_no_pump_exits_before_generic_time_stop() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_pumpswap_profit",
        "gate_profile": "pumpswap_profit_broad",
        "opened_at": now - dt.timedelta(minutes=3, seconds=10),
        "buy_price_usd": 1.0,
        "highest_pnl_pct": 1.5,
        "partial_taken": False,
    }

    reason = exit_policy.should_exit(subject, price_now=1.0, now=now, pnl_pct=0.0)
    assert reason == "NO_PUMP_EXIT"


def test_green_sniper_uses_runner_profile_and_later_partial() -> None:
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_green_candle_sniper",
        "gate_profile": "green_sniper",
        "highest_pnl_pct": 125.0,
        "partial_taken": False,
    }

    policy = exit_policy.effective_exit_policy(subject)

    assert policy.runner_exit_profile == "green_sniper_runner"
    assert policy.tp_partial_trigger_pct == 25.0
    assert policy.tp_partial_fraction == 0.25
    assert policy.post_partial_lock_floor_pct == 80.0
    assert policy.post_partial_max_giveback_pct == 10.0


def test_green_sniper_does_not_full_take_profit_before_partial() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_green_candle_sniper",
        "gate_profile": "green_sniper",
        "opened_at": now - dt.timedelta(seconds=70),
        "buy_price_usd": 1.0,
        "highest_pnl_pct": 20.0,
        "partial_taken": False,
    }

    reason = exit_policy.should_exit(subject, price_now=1.20, now=now, pnl_pct=20.0)

    assert reason is None


def test_green_sniper_adverse_tick_uses_fast_window() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_green_candle_sniper",
        "gate_profile": "green_sniper",
        "opened_at": now - dt.timedelta(seconds=50),
        "buy_price_usd": 1.0,
        "highest_pnl_pct": 0.0,
        "partial_taken": False,
    }

    reason = exit_policy.should_exit(subject, price_now=0.89, now=now, pnl_pct=-11.0)

    assert reason == "ADVERSE_TICK"


def test_green_sniper_post_partial_protection_precedes_adverse_tick() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_green_candle_sniper",
        "gate_profile": "green_sniper",
        "opened_at": now - dt.timedelta(minutes=40),
        "buy_price_usd": 1.0,
        "highest_pnl_pct": 251.0,
        "partial_taken": True,
    }

    reason = exit_policy.should_exit(subject, price_now=0.70, now=now, pnl_pct=-30.0)

    assert reason == "POST_PARTIAL_TRAILING"


def test_prime_runner_escalates_lock_floor_after_peak_threshold() -> None:
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_pumpswap_profit",
        "gate_profile": "pumpswap_profit_prime",
        "buy_market_cap_usd": 20_000.0,
        "buy_price_pct_5m": 70.0,
        "buy_txns_last_5m": 180.0,
        "buy_liquidity_is_proxy": 0,
        "highest_pnl_pct": 95.0,
        "partial_taken": True,
    }

    policy = exit_policy.effective_exit_policy(subject)

    assert policy.runner_exit_profile == "prime_runner"
    assert policy.runner_profile_state == "step"
    assert policy.post_partial_lock_floor_pct == 45.0
    assert policy.post_partial_max_giveback_pct == 15.0


def test_meteor_runner_uses_highest_lock_floor_after_big_peak() -> None:
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_pumpswap_profit",
        "gate_profile": "pumpswap_meteor_prime",
        "buy_market_cap_usd": 18_000.0,
        "buy_price_pct_5m": 180.0,
        "buy_txns_last_5m": 500.0,
        "buy_liquidity_is_proxy": 0,
        "highest_pnl_pct": 320.0,
        "partial_taken": True,
    }

    policy = exit_policy.effective_exit_policy(subject)

    assert policy.runner_exit_profile == "meteor_runner"
    assert policy.runner_profile_state == "step2"
    assert policy.post_partial_lock_floor_pct == 120.0
    assert policy.post_partial_max_giveback_pct == 20.0


def test_breakout_probe_uses_meteor_runner_profile() -> None:
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_pumpswap_breakout_probe",
        "gate_profile": "pumpswap_breakout_probe",
        "highest_pnl_pct": 140.0,
        "partial_taken": True,
    }

    policy = exit_policy.effective_exit_policy(subject)

    assert policy.runner_exit_profile == "meteor_runner"
    assert policy.runner_profile_state == "step1"
    assert policy.post_partial_lock_floor_pct == 70.0
    assert policy.post_partial_max_giveback_pct == 20.0
    assert exit_policy.partial_fraction(subject) == 0.50


def test_extreme_momentum_broad_uses_meteor_runner_profile() -> None:
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_pumpswap_profit",
        "gate_profile": "pumpswap_profit_broad",
        "buy_dex_id": "pumpswap",
        "buy_market_cap_usd": 90_000.0,
        "buy_price_pct_5m": 240.0,
        "buy_txns_last_5m": 900.0,
        "buy_liquidity_is_proxy": 0,
        "highest_pnl_pct": 260.0,
        "partial_taken": True,
    }

    policy = exit_policy.effective_exit_policy(subject)

    assert policy.runner_exit_profile == "meteor_runner"
    assert policy.runner_profile_state == "step2"
    assert exit_policy.partial_fraction(subject) == 0.50


def test_aggressive_research_hot_low_mcap_stays_broad_runner_fraction() -> None:
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_sniper_research",
        "gate_profile": "paper_aggressive_research_buy",
        "buy_dex_id": "pumpfun",
        "buy_market_cap_usd": 9_500.0,
        "buy_price_pct_5m": 220.0,
        "buy_txns_last_5m": 180.0,
        "highest_pnl_pct": 120.0,
        "partial_taken": False,
    }

    policy = exit_policy.effective_exit_policy(subject)

    assert policy.runner_exit_profile == "broad_runner"
    assert exit_policy.partial_fraction(subject) == 0.80


def test_research_rank_pumpswap_runner_uses_jackpot_profile() -> None:
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_sniper_research",
        "gate_profile": "pumpswap_profit_research",
        "profit_lane_tier": "pump_early_research_rank_canary",
        "buy_dex_id": "pumpswap",
        "buy_liquidity_is_proxy": 0,
        "buy_liquidity_usd": 19_316.86,
        "buy_market_cap_usd": 61_534.0,
        "buy_price_pct_5m": 46.14,
        "buy_txns_last_5m": 784.0,
        "research_rank_score": 68.5,
        "highest_pnl_pct": 628.0,
        "partial_taken": True,
    }

    policy = exit_policy.effective_exit_policy(subject)

    assert policy.runner_exit_profile == "jackpot_runner"
    assert policy.runner_profile_state == "step3"
    assert policy.post_partial_lock_floor_pct == 320.0
    assert policy.post_partial_max_giveback_pct == 120.0
    assert exit_policy.partial_fraction(subject) == 0.35


def test_research_rank_canary_real_lane_uses_jackpot_ladder() -> None:
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_research_rank_canary",
        "gate_profile": "research_rank_canary",
        "buy_dex_id": "pumpswap",
        "buy_liquidity_is_proxy": 0,
        "buy_liquidity_usd": 22_000.0,
        "buy_market_cap_usd": 38_000.0,
        "buy_price_pct_5m": 40.0,
        "buy_txns_last_5m": 320.0,
        "research_rank_score": 60.0,
        "highest_pnl_pct": 500.0,
        "partial_taken": False,
        "entry_qty": 1_000,
        "qty": 1_000,
        "realized_qty": 0,
    }

    policy = exit_policy.effective_exit_policy(subject)
    plan = exit_policy.partial_ladder_plan(subject, 500.0)

    assert policy.runner_exit_profile == "jackpot_runner"
    assert plan["enabled"] is True
    assert plan["target_secured_fraction"] == pytest.approx(0.55)
    assert exit_policy.partial_sell_fraction(subject, 500.0) == pytest.approx(0.55)


def test_jackpot_runner_waits_for_first_ladder_step_before_take_profit() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_research_rank_canary",
        "gate_profile": "research_rank_canary",
        "buy_dex_id": "pumpswap",
        "buy_liquidity_is_proxy": 0,
        "buy_liquidity_usd": 22_000.0,
        "buy_market_cap_usd": 38_000.0,
        "buy_price_pct_5m": 40.0,
        "buy_txns_last_5m": 320.0,
        "research_rank_score": 60.0,
        "opened_at": now,
        "buy_price_usd": 1.0,
        "highest_pnl_pct": 40.0,
        "partial_taken": False,
        "entry_qty": 1_000,
        "qty": 1_000,
        "realized_qty": 0,
    }

    policy = exit_policy.effective_exit_policy(subject)

    assert policy.take_profit_pct == 100.0
    assert exit_policy.should_take_partial(subject, 40.0) is False
    assert exit_policy.should_exit(subject, price_now=1.4, now=now, pnl_pct=40.0) is None


def test_green_birth_probe_moonshot_ladder_keeps_large_moonbag() -> None:
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_green_candle_sniper",
        "gate_profile": "green_sniper_birth_probe",
        "buy_dex_id": "pumpfun",
        "highest_pnl_pct": 700.0,
        "partial_taken": False,
        "entry_qty": 1_000,
        "qty": 1_000,
        "realized_qty": 0,
    }

    policy = exit_policy.effective_exit_policy(subject)
    plan = exit_policy.partial_ladder_plan(subject, 700.0)

    assert policy.runner_exit_profile == "green_sniper_runner"
    assert plan["target_secured_fraction"] == pytest.approx(0.60)
    assert exit_policy.partial_sell_fraction(subject, 700.0) == pytest.approx(0.60)


def test_research_rank_jackpot_profile_protects_without_rank_column() -> None:
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_sniper_research",
        "gate_profile": "pumpswap_profit_research",
        "buy_dex_id": "pumpswap",
        "buy_liquidity_is_proxy": 0,
        "buy_liquidity_usd": 21_979.31,
        "buy_market_cap_usd": 77_639.0,
        "buy_price_pct_5m": 92.8,
        "buy_txns_last_5m": 595.0,
        "highest_pnl_pct": 56.0,
        "partial_taken": True,
    }

    policy = exit_policy.effective_exit_policy(subject)

    assert policy.runner_exit_profile == "jackpot_runner"
    assert policy.runner_profile_state == "base"
    assert policy.post_partial_lock_floor_pct == 35.0
    assert policy.post_partial_max_giveback_pct == 12.0


def test_aggressive_research_low_mcap_stays_broad_runner_fraction() -> None:
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_sniper_research",
        "gate_profile": "live_aggressive_research_buy",
        "buy_dex_id": "pumpfun",
        "buy_market_cap_usd": 12_000.0,
        "buy_price_pct_5m": -15.0,
        "buy_txns_last_5m": 40.0,
        "highest_pnl_pct": 20.0,
        "partial_taken": False,
    }

    policy = exit_policy.effective_exit_policy(subject)

    assert policy.runner_exit_profile == "broad_runner"
    assert exit_policy.partial_fraction(subject) == 0.80
