from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from typing import Any

from analytics import bird_runner_exit
from config.config import CFG, PROJECT_ROOT


_RUNTIME_DRY_RUN_OVERRIDE: bool | None = None


def set_runtime_dry_run(dry_run: bool | None) -> None:
    global _RUNTIME_DRY_RUN_OVERRIDE
    _RUNTIME_DRY_RUN_OVERRIDE = None if dry_run is None else bool(dry_run)


def _get(subject: Any, key: str, default: Any = None) -> Any:
    if isinstance(subject, dict):
        return subject.get(key, default)
    return getattr(subject, key, default)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        out = float(value)
        if out != out or out == float("inf") or out == float("-inf"):
            return float(default)
        return out
    except Exception:
        return float(default)


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _effective_dry_run(subject: Any | None = None) -> bool:
    if subject is not None:
        explicit = _get(subject, "dry_run", None)
        if explicit is not None:
            return _to_bool(explicit, bool(getattr(CFG, "DRY_RUN", True)))
    if _RUNTIME_DRY_RUN_OVERRIDE is not None:
        return bool(_RUNTIME_DRY_RUN_OVERRIDE)
    return bool(getattr(CFG, "DRY_RUN", True))


def _normalize_regime(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"pump_early", "pumpfun", "pump", "pump_fun"}:
        return "pump_early"
    if raw in {"revival", "revive", "revived"}:
        return "revival"
    return "dex_mature"


def _opt_env_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        return float(raw)
    except Exception:
        return None


@dataclass(frozen=True)
class ExitPolicy:
    regime: str
    take_profit_pct: float
    stop_loss_pct: float
    trailing_pct: float
    max_holding_h: float
    max_hard_hold_h: float
    tp_partial_enabled: bool
    tp_partial_trigger_pct: float
    tp_partial_fraction: float
    post_partial_stop_pct: float
    post_partial_trailing_pct: float
    post_partial_protection_enabled: bool
    post_partial_lock_floor_pct: float
    post_partial_max_giveback_pct: float
    pre_partial_time_stop_min: float
    pre_partial_time_stop_max_pnl_pct: float
    pre_partial_time_stop_min_peak_pct: float
    pre_partial_retrace_trigger_pct: float
    pre_partial_retrace_giveback_pct: float
    pre_partial_retrace_floor_pct: float
    early_drop_kill_pct: float
    early_drop_window_min: float
    liq_crush_fraction: float
    liq_crush_drop_pct: float
    liq_crush_window_min: float
    liq_crush_abs_fract: float
    no_expansion_max_pct: float
    no_pump_window_min: float
    no_pump_min_pnl_pct: float
    no_pump_max_pnl_pct: float | None
    time_stop_min: float
    time_stop_max_pnl_pct: float
    time_stop_min_peak_pct: float
    runner_exit_profile: str | None = None
    runner_profile_state: str | None = None


_REGIME_EXIT_OVERRIDES: dict[str, dict[str, Any]] = {
    "pump_early": {
        "take_profit_pct": CFG.PUMP_EARLY_TAKE_PROFIT_PCT,
        "stop_loss_pct": CFG.PUMP_EARLY_STOP_LOSS_PCT,
        "trailing_pct": CFG.PUMP_EARLY_TRAILING_PCT,
        "max_holding_h": CFG.PUMP_EARLY_MAX_HOLDING_H,
        "max_hard_hold_h": CFG.PUMP_EARLY_MAX_HARD_HOLD_H,
        "tp_partial_trigger_pct": CFG.PUMP_EARLY_TP_PARTIAL_TRIGGER_PCT,
        "tp_partial_fraction": CFG.PUMP_EARLY_TP_PARTIAL_FRACTION,
        "post_partial_stop_pct": CFG.PUMP_EARLY_POST_PARTIAL_STOP_PCT,
        "post_partial_trailing_pct": CFG.PUMP_EARLY_POST_PARTIAL_TRAILING_PCT,
        "post_partial_protection_enabled": CFG.PUMP_EARLY_POST_PARTIAL_PROTECTION_ENABLED,
        "post_partial_lock_floor_pct": CFG.PUMP_EARLY_POST_PARTIAL_LOCK_FLOOR_PCT,
        "post_partial_max_giveback_pct": CFG.PUMP_EARLY_POST_PARTIAL_MAX_GIVEBACK_PCT,
        "pre_partial_time_stop_min": CFG.PUMP_EARLY_PRE_PARTIAL_TIME_STOP_MIN,
        "pre_partial_time_stop_max_pnl_pct": CFG.PUMP_EARLY_PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT,
        "pre_partial_time_stop_min_peak_pct": CFG.PUMP_EARLY_PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT,
        "pre_partial_retrace_trigger_pct": CFG.PUMP_EARLY_PRE_PARTIAL_RETRACE_TRIGGER_PCT,
        "pre_partial_retrace_giveback_pct": CFG.PUMP_EARLY_PRE_PARTIAL_RETRACE_GIVEBACK_PCT,
        "pre_partial_retrace_floor_pct": CFG.PUMP_EARLY_PRE_PARTIAL_RETRACE_FLOOR_PCT,
        "early_drop_kill_pct": _opt_env_float("PUMP_EARLY_EARLY_DROP_KILL_PCT"),
        "early_drop_window_min": _opt_env_float("PUMP_EARLY_EARLY_DROP_WINDOW_MIN"),
        "no_pump_window_min": CFG.PUMP_EARLY_NO_PUMP_WINDOW_MIN,
        "no_pump_min_pnl_pct": CFG.PUMP_EARLY_NO_PUMP_MIN_PNL_PCT,
        "no_pump_max_pnl_pct": CFG.PUMP_EARLY_NO_PUMP_MAX_PNL_PCT,
        "time_stop_min": CFG.PUMP_EARLY_TIME_STOP_MIN,
        "time_stop_max_pnl_pct": CFG.PUMP_EARLY_TIME_STOP_MAX_PNL_PCT,
        "time_stop_min_peak_pct": CFG.PUMP_EARLY_TIME_STOP_MIN_PEAK_PCT,
    },
    "dex_mature": {
        "take_profit_pct": CFG.DEX_MATURE_TAKE_PROFIT_PCT,
        "stop_loss_pct": CFG.DEX_MATURE_STOP_LOSS_PCT,
        "trailing_pct": CFG.DEX_MATURE_TRAILING_PCT,
        "max_holding_h": CFG.DEX_MATURE_MAX_HOLDING_H,
        "max_hard_hold_h": CFG.DEX_MATURE_MAX_HARD_HOLD_H,
        "tp_partial_trigger_pct": CFG.DEX_MATURE_TP_PARTIAL_TRIGGER_PCT,
        "tp_partial_fraction": CFG.DEX_MATURE_TP_PARTIAL_FRACTION,
        "post_partial_stop_pct": CFG.DEX_MATURE_POST_PARTIAL_STOP_PCT,
        "post_partial_trailing_pct": CFG.DEX_MATURE_POST_PARTIAL_TRAILING_PCT,
        "post_partial_protection_enabled": CFG.DEX_MATURE_POST_PARTIAL_PROTECTION_ENABLED,
        "post_partial_lock_floor_pct": CFG.DEX_MATURE_POST_PARTIAL_LOCK_FLOOR_PCT,
        "post_partial_max_giveback_pct": CFG.DEX_MATURE_POST_PARTIAL_MAX_GIVEBACK_PCT,
        "pre_partial_time_stop_min": CFG.DEX_MATURE_PRE_PARTIAL_TIME_STOP_MIN,
        "pre_partial_time_stop_max_pnl_pct": CFG.DEX_MATURE_PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT,
        "pre_partial_time_stop_min_peak_pct": CFG.DEX_MATURE_PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT,
        "pre_partial_retrace_trigger_pct": CFG.DEX_MATURE_PRE_PARTIAL_RETRACE_TRIGGER_PCT,
        "pre_partial_retrace_giveback_pct": CFG.DEX_MATURE_PRE_PARTIAL_RETRACE_GIVEBACK_PCT,
        "pre_partial_retrace_floor_pct": CFG.DEX_MATURE_PRE_PARTIAL_RETRACE_FLOOR_PCT,
        "early_drop_kill_pct": _opt_env_float("DEX_MATURE_EARLY_DROP_KILL_PCT"),
        "early_drop_window_min": _opt_env_float("DEX_MATURE_EARLY_DROP_WINDOW_MIN"),
        "no_pump_window_min": CFG.DEX_MATURE_NO_PUMP_WINDOW_MIN,
        "no_pump_min_pnl_pct": CFG.DEX_MATURE_NO_PUMP_MIN_PNL_PCT,
        "no_pump_max_pnl_pct": CFG.DEX_MATURE_NO_PUMP_MAX_PNL_PCT,
        "time_stop_min": CFG.DEX_MATURE_TIME_STOP_MIN,
        "time_stop_max_pnl_pct": CFG.DEX_MATURE_TIME_STOP_MAX_PNL_PCT,
        "time_stop_min_peak_pct": CFG.DEX_MATURE_TIME_STOP_MIN_PEAK_PCT,
    },
    "revival": {
        "take_profit_pct": CFG.REVIVAL_TAKE_PROFIT_PCT,
        "stop_loss_pct": CFG.REVIVAL_STOP_LOSS_PCT,
        "trailing_pct": CFG.REVIVAL_TRAILING_PCT,
        "max_holding_h": CFG.REVIVAL_MAX_HOLDING_H,
        "max_hard_hold_h": CFG.REVIVAL_MAX_HARD_HOLD_H,
        "tp_partial_trigger_pct": CFG.REVIVAL_TP_PARTIAL_TRIGGER_PCT,
        "tp_partial_fraction": CFG.REVIVAL_TP_PARTIAL_FRACTION,
        "post_partial_stop_pct": CFG.REVIVAL_POST_PARTIAL_STOP_PCT,
        "post_partial_trailing_pct": CFG.REVIVAL_POST_PARTIAL_TRAILING_PCT,
        "post_partial_protection_enabled": CFG.REVIVAL_POST_PARTIAL_PROTECTION_ENABLED,
        "post_partial_lock_floor_pct": CFG.REVIVAL_POST_PARTIAL_LOCK_FLOOR_PCT,
        "post_partial_max_giveback_pct": CFG.REVIVAL_POST_PARTIAL_MAX_GIVEBACK_PCT,
        "pre_partial_time_stop_min": CFG.REVIVAL_PRE_PARTIAL_TIME_STOP_MIN,
        "pre_partial_time_stop_max_pnl_pct": CFG.REVIVAL_PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT,
        "pre_partial_time_stop_min_peak_pct": CFG.REVIVAL_PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT,
        "pre_partial_retrace_trigger_pct": CFG.REVIVAL_PRE_PARTIAL_RETRACE_TRIGGER_PCT,
        "pre_partial_retrace_giveback_pct": CFG.REVIVAL_PRE_PARTIAL_RETRACE_GIVEBACK_PCT,
        "pre_partial_retrace_floor_pct": CFG.REVIVAL_PRE_PARTIAL_RETRACE_FLOOR_PCT,
        "early_drop_kill_pct": _opt_env_float("REVIVAL_EARLY_DROP_KILL_PCT"),
        "early_drop_window_min": _opt_env_float("REVIVAL_EARLY_DROP_WINDOW_MIN"),
        "no_pump_window_min": CFG.REVIVAL_NO_PUMP_WINDOW_MIN,
        "no_pump_min_pnl_pct": CFG.REVIVAL_NO_PUMP_MIN_PNL_PCT,
        "no_pump_max_pnl_pct": CFG.REVIVAL_NO_PUMP_MAX_PNL_PCT,
        "time_stop_min": CFG.REVIVAL_TIME_STOP_MIN,
        "time_stop_max_pnl_pct": CFG.REVIVAL_TIME_STOP_MAX_PNL_PCT,
        "time_stop_min_peak_pct": CFG.REVIVAL_TIME_STOP_MIN_PEAK_PCT,
    },
}


def resolve_entry_regime(subject: Any) -> str:
    explicit = _get(subject, "entry_regime")
    if explicit is not None and str(explicit).strip():
        return _normalize_regime(explicit)
    discovered_via = _get(subject, "discovered_via")
    if discovered_via is None:
        return "dex_mature"
    raw = str(discovered_via).strip().lower()
    if raw in {"pumpfun", "pump", "pump_fun"}:
        return "pump_early"
    if raw in {"revival", "revive", "revived"}:
        return "revival"
    return "dex_mature"


def _override_value(regime: str, key: str, default: float) -> float:
    if not bool(CFG.EXIT_PROFILE_BY_REGIME):
        return float(default)
    override = _REGIME_EXIT_OVERRIDES.get(regime, {}).get(key)
    return float(default if override is None else override)


def _override_bool(regime: str, key: str, default: bool) -> bool:
    if not bool(CFG.EXIT_PROFILE_BY_REGIME):
        return bool(default)
    override = _REGIME_EXIT_OVERRIDES.get(regime, {}).get(key)
    return _to_bool(default if override is None else override, default)


def _override_optional_value(regime: str, key: str, default: float | None) -> float | None:
    if not bool(CFG.EXIT_PROFILE_BY_REGIME):
        override = None
    else:
        override = _REGIME_EXIT_OVERRIDES.get(regime, {}).get(key)
    value = default if override is None else override
    return None if value is None else float(value)


def _profit_runner_profiles() -> dict[str, dict[str, float]]:
    return {
        "bird_runner": {
            "lock_floor_pct": float(getattr(CFG, "BIRD_TP1_PCT", 25.0) or 25.0),
            "partial_fraction": float(getattr(CFG, "BIRD_TP1_FRACTION", 0.25) or 0.25),
            "max_giveback_pct": float(getattr(CFG, "BIRD_MAX_GIVEBACK_PCT", 12.0) or 12.0),
        },
        "green_sniper_runner": {
            "partial_fraction": float(getattr(CFG, "GREEN_SNIPER_TP_PARTIAL_FRACTION", 0.25) or 0.25),
            "step1_peak_pct": float(getattr(CFG, "GREEN_SNIPER_STEP1_PEAK_PCT", 60.0) or 60.0),
            "step1_lock_floor_pct": float(getattr(CFG, "GREEN_SNIPER_STEP1_LOCK_FLOOR_PCT", 20.0) or 20.0),
            "step1_max_giveback_pct": float(getattr(CFG, "GREEN_SNIPER_STEP1_MAX_GIVEBACK_PCT", 5.0) or 5.0),
            "step2_peak_pct": float(getattr(CFG, "GREEN_SNIPER_STEP2_PEAK_PCT", 120.0) or 120.0),
            "step2_lock_floor_pct": float(getattr(CFG, "GREEN_SNIPER_STEP2_LOCK_FLOOR_PCT", 80.0) or 80.0),
            "step2_max_giveback_pct": float(getattr(CFG, "GREEN_SNIPER_STEP2_MAX_GIVEBACK_PCT", 10.0) or 10.0),
            "step3_peak_pct": float(getattr(CFG, "GREEN_SNIPER_STEP3_PEAK_PCT", 250.0) or 250.0),
            "step3_lock_floor_pct": float(getattr(CFG, "GREEN_SNIPER_STEP3_LOCK_FLOOR_PCT", 160.0) or 160.0),
            "step3_max_giveback_pct": float(getattr(CFG, "GREEN_SNIPER_STEP3_MAX_GIVEBACK_PCT", 20.0) or 20.0),
            "step4_peak_pct": float(getattr(CFG, "GREEN_SNIPER_STEP4_PEAK_PCT", 700.0) or 700.0),
            "step4_lock_floor_pct": float(getattr(CFG, "GREEN_SNIPER_STEP4_LOCK_FLOOR_PCT", 420.0) or 420.0),
            "step4_max_giveback_pct": float(getattr(CFG, "GREEN_SNIPER_STEP4_MAX_GIVEBACK_PCT", 220.0) or 220.0),
            "step5_peak_pct": float(getattr(CFG, "GREEN_SNIPER_STEP5_PEAK_PCT", 1500.0) or 1500.0),
            "step5_lock_floor_pct": float(getattr(CFG, "GREEN_SNIPER_STEP5_LOCK_FLOOR_PCT", 900.0) or 900.0),
            "step5_max_giveback_pct": float(getattr(CFG, "GREEN_SNIPER_STEP5_MAX_GIVEBACK_PCT", 450.0) or 450.0),
        },
        "broad_runner": {
            "lock_floor_pct": float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_BROAD_LOCK_FLOOR_PCT", 20.0) or 20.0),
            "partial_fraction": float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_BROAD_PARTIAL_FRACTION", 0.80) or 0.80),
            "max_giveback_pct": float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_BROAD_MAX_GIVEBACK_PCT", 5.0) or 5.0),
        },
        "prime_runner": {
            "base_lock_floor_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_LOCK_FLOOR_PCT", 25.0) or 25.0
            ),
            "partial_fraction": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_PRIME_PARTIAL_FRACTION", 0.65) or 0.65
            ),
            "base_max_giveback_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_MAX_GIVEBACK_PCT", 10.0) or 10.0
            ),
            "step_peak_pct": float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_PEAK_PCT", 80.0) or 80.0),
            "step_lock_floor_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_LOCK_FLOOR_PCT", 45.0) or 45.0
            ),
            "step_max_giveback_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_MAX_GIVEBACK_PCT", 15.0) or 15.0
            ),
        },
        "meteor_runner": {
            "base_lock_floor_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_LOCK_FLOOR_PCT", 25.0) or 25.0
            ),
            "partial_fraction": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_PARTIAL_FRACTION", 0.50) or 0.50
            ),
            "base_max_giveback_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_MAX_GIVEBACK_PCT", 15.0) or 15.0
            ),
            "step1_peak_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_PEAK_PCT", 100.0) or 100.0
            ),
            "step1_lock_floor_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_LOCK_FLOOR_PCT", 70.0) or 70.0
            ),
            "step1_max_giveback_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_MAX_GIVEBACK_PCT", 20.0) or 20.0
            ),
            "step2_peak_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP2_PEAK_PCT", 250.0) or 250.0
            ),
            "step2_lock_floor_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP2_LOCK_FLOOR_PCT", 120.0) or 120.0
            ),
        },
        "jackpot_runner": {
            "partial_fraction": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_PARTIAL_FRACTION", 0.35) or 0.35
            ),
            "base_lock_floor_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_BASE_LOCK_FLOOR_PCT", 35.0) or 35.0
            ),
            "base_max_giveback_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_BASE_MAX_GIVEBACK_PCT", 12.0) or 12.0
            ),
            "step1_peak_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_PEAK_PCT", 100.0) or 100.0
            ),
            "step1_lock_floor_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_LOCK_FLOOR_PCT", 80.0) or 80.0
            ),
            "step1_max_giveback_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_MAX_GIVEBACK_PCT", 18.0) or 18.0
            ),
            "step2_peak_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_PEAK_PCT", 300.0) or 300.0
            ),
            "step2_lock_floor_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_LOCK_FLOOR_PCT", 180.0) or 180.0
            ),
            "step2_max_giveback_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_MAX_GIVEBACK_PCT", 25.0) or 25.0
            ),
            "step3_peak_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_PEAK_PCT", 500.0) or 500.0
            ),
            "step3_lock_floor_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_LOCK_FLOOR_PCT", 320.0) or 320.0
            ),
            "step3_max_giveback_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_MAX_GIVEBACK_PCT", 120.0) or 120.0
            ),
            "step4_peak_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_PEAK_PCT", 1000.0) or 1000.0
            ),
            "step4_lock_floor_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_LOCK_FLOOR_PCT", 650.0) or 650.0
            ),
            "step4_max_giveback_pct": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_MAX_GIVEBACK_PCT", 220.0) or 220.0
            ),
        },
    }


def _subject_real_liquidity(subject: Any) -> bool:
    return not any(
        (
            _to_bool(_get(subject, "buy_liquidity_is_proxy"), False),
            _to_bool(_get(subject, "liquidity_is_proxy"), False),
            _to_bool(_get(subject, "liquidity_usd_is_proxy"), False),
        )
    )


def _subject_dex_id(subject: Any) -> str:
    raw = _get(subject, "buy_dex_id") or _get(subject, "dex_id") or _get(subject, "dexId")
    return str(raw or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def _is_aggressive_research_subject(subject: Any) -> bool:
    lane = str(_get(subject, "entry_lane", "") or "").strip().lower()
    profile = str(_get(subject, "gate_profile", "") or _get(subject, "sniper_gate_profile", "") or "").strip().lower()
    return lane == "pump_early_sniper_research" or profile in {
        "paper_aggressive_research_buy",
        "live_aggressive_research_buy",
    }


def _resolve_aggressive_research_runner_profile(subject: Any) -> str:
    if _is_jackpot_research_subject(subject):
        return "jackpot_runner"
    return "broad_runner"


def resolve_runner_exit_profile(subject: Any) -> str | None:
    explicit = str(_get(subject, "runner_exit_profile", "") or "").strip().lower()
    if explicit in {"broad_runner", "prime_runner", "meteor_runner", "jackpot_runner", "green_sniper_runner", "bird_runner"}:
        return explicit
    if _is_green_sniper_subject(subject):
        return "green_sniper_runner"
    if _is_research_rank_subject(subject):
        if _is_jackpot_research_subject(subject):
            return "jackpot_runner"
        return "prime_runner"
    if _is_aggressive_research_subject(subject):
        return _resolve_aggressive_research_runner_profile(subject)
    if not _is_pumpswap_profit_subject(subject):
        return None

    lane = str(_get(subject, "entry_lane", "") or "").strip().lower()
    profile = str(_get(subject, "gate_profile", "") or _get(subject, "sniper_gate_profile", "") or "").strip().lower()
    mcap = _to_float(_get(subject, "buy_market_cap_usd"))
    if mcap <= 0:
        mcap = _to_float(_get(subject, "market_cap_usd"))
    price5m = _to_float(_get(subject, "buy_price_pct_5m"))
    if price5m is None:
        price5m = _to_float(_get(subject, "price_pct_5m"))
    txns_5m = _to_float(_get(subject, "buy_txns_last_5m"), 0.0)
    if txns_5m <= 0:
        txns_5m = _to_float(_get(subject, "txns_last_5m"), 0.0)

    if profile.startswith("pumpswap_meteor"):
        return "meteor_runner"
    if profile.startswith("pumpswap_breakout") or lane == "pump_early_pumpswap_breakout_probe":
        return "meteor_runner"
    if (
        _subject_real_liquidity(subject)
        and _subject_dex_id(subject) == "pumpswap"
        and price5m is not None
        and price5m >= float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_MOMENTUM_PRICE5M_PCT", 180.0) or 180.0)
        and txns_5m >= float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_MOMENTUM_MIN_TXNS_5M", 600) or 600)
    ):
        return "meteor_runner"
    if profile.startswith("pumpswap_profit_prime"):
        if (
            _subject_real_liquidity(subject)
            and mcap > 0
            and mcap < 25_000.0
            and price5m is not None
            and price5m >= 110.0
            and txns_5m >= 220.0
        ):
            return "meteor_runner"
        return "prime_runner"
    return "broad_runner"


def _runner_policy_overrides(subject: Any, peak_pct: float) -> tuple[str | None, str | None, float | None, float | None]:
    profile = resolve_runner_exit_profile(subject)
    if profile is None:
        return None, None, None, None

    profiles = _profit_runner_profiles()
    peak = max(0.0, float(peak_pct))

    if profile == "prime_runner":
        lock_floor = float(profiles["prime_runner"]["base_lock_floor_pct"])
        max_giveback = float(profiles["prime_runner"]["base_max_giveback_pct"])
        state = "base"
        if peak >= float(profiles["prime_runner"]["step_peak_pct"]):
            lock_floor = float(profiles["prime_runner"]["step_lock_floor_pct"])
            max_giveback = float(profiles["prime_runner"]["step_max_giveback_pct"])
            state = "step"
        return profile, state, lock_floor, max_giveback

    if profile == "meteor_runner":
        lock_floor = float(profiles["meteor_runner"]["base_lock_floor_pct"])
        max_giveback = float(profiles["meteor_runner"]["base_max_giveback_pct"])
        state = "base"
        if peak >= float(profiles["meteor_runner"]["step1_peak_pct"]):
            lock_floor = float(profiles["meteor_runner"]["step1_lock_floor_pct"])
            max_giveback = float(profiles["meteor_runner"]["step1_max_giveback_pct"])
            state = "step1"
        if peak >= float(profiles["meteor_runner"]["step2_peak_pct"]):
            lock_floor = max(lock_floor, float(profiles["meteor_runner"]["step2_lock_floor_pct"]))
            state = "step2"
        return profile, state, lock_floor, max_giveback

    if profile == "jackpot_runner":
        cfg = profiles["jackpot_runner"]
        lock_floor = float(cfg["base_lock_floor_pct"])
        max_giveback = float(cfg["base_max_giveback_pct"])
        state = "base"
        if peak >= float(cfg["step1_peak_pct"]):
            lock_floor = float(cfg["step1_lock_floor_pct"])
            max_giveback = float(cfg["step1_max_giveback_pct"])
            state = "step1"
        if peak >= float(cfg["step2_peak_pct"]):
            lock_floor = float(cfg["step2_lock_floor_pct"])
            max_giveback = float(cfg["step2_max_giveback_pct"])
            state = "step2"
        if peak >= float(cfg["step3_peak_pct"]):
            lock_floor = float(cfg["step3_lock_floor_pct"])
            max_giveback = float(cfg["step3_max_giveback_pct"])
            state = "step3"
        if peak >= float(cfg["step4_peak_pct"]):
            lock_floor = float(cfg["step4_lock_floor_pct"])
            max_giveback = float(cfg["step4_max_giveback_pct"])
            state = "step4"
        return profile, state, lock_floor, max_giveback

    if profile == "green_sniper_runner":
        cfg = profiles["green_sniper_runner"]
        lock_floor = 0.0
        max_giveback = float(cfg["step1_max_giveback_pct"])
        state = "pre_step"
        if peak >= float(cfg["step1_peak_pct"]):
            lock_floor = float(cfg["step1_lock_floor_pct"])
            max_giveback = float(cfg["step1_max_giveback_pct"])
            state = "step1"
        if peak >= float(cfg["step2_peak_pct"]):
            lock_floor = float(cfg["step2_lock_floor_pct"])
            max_giveback = float(cfg["step2_max_giveback_pct"])
            state = "step2"
        if peak >= float(cfg["step3_peak_pct"]):
            lock_floor = float(cfg["step3_lock_floor_pct"])
            max_giveback = float(cfg["step3_max_giveback_pct"])
            state = "step3"
        if peak >= float(cfg["step4_peak_pct"]):
            lock_floor = float(cfg["step4_lock_floor_pct"])
            max_giveback = float(cfg["step4_max_giveback_pct"])
            state = "step4"
        if peak >= float(cfg["step5_peak_pct"]):
            lock_floor = float(cfg["step5_lock_floor_pct"])
            max_giveback = float(cfg["step5_max_giveback_pct"])
            state = "step5"
        return profile, state, lock_floor, max_giveback

    if profile == "bird_runner":
        cfg = profiles["bird_runner"]
        return (
            "bird_runner",
            "base",
            float(cfg["lock_floor_pct"]),
            float(cfg["max_giveback_pct"]),
        )

    return (
        "broad_runner",
        "base",
        float(profiles["broad_runner"]["lock_floor_pct"]),
        float(profiles["broad_runner"]["max_giveback_pct"]),
    )


def _is_pumpswap_profit_subject(subject: Any) -> bool:
    lane = str(_get(subject, "entry_lane", "") or "").strip().lower()
    profile = str(_get(subject, "gate_profile", "") or _get(subject, "sniper_gate_profile", "") or "").strip().lower()
    bucket = str(_get(subject, "size_bucket", "") or "").strip().lower()
    return (
        lane == "pump_early_pumpswap_profit"
        or lane == "pump_early_pumpswap_breakout_probe"
        or _is_aggressive_research_subject(subject)
        or profile.startswith("pumpswap_profit")
        or profile.startswith("pumpswap_meteor")
        or profile.startswith("pumpswap_breakout")
        or bucket in {"pumpswap_profit", "pumpswap_prime", "pumpswap_meteor", "pumpswap_breakout"}
    )


def _is_jackpot_research_subject(subject: Any) -> bool:
    if not bool(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_ENABLED", True)):
        return False

    lane = str(_get(subject, "entry_lane", "") or "").strip().lower()
    profile = str(_get(subject, "gate_profile", "") or _get(subject, "sniper_gate_profile", "") or "").strip().lower()
    tier = str(_get(subject, "profit_lane_tier", "") or "").strip().lower()
    if not (
        lane in {"pump_early_sniper_research", "pump_early_research_rank_canary"}
        or tier == "pump_early_research_rank_canary"
        or profile in {"pumpswap_profit_research", "research_rank_canary"}
    ):
        return False
    if not _subject_real_liquidity(subject):
        return False

    liquidity = _to_float(_get(subject, "buy_liquidity_usd"), 0.0)
    if liquidity <= 0:
        liquidity = _to_float(_get(subject, "liquidity_usd"), 0.0)
    if liquidity < float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_LIQUIDITY_USD", 10_000.0) or 10_000.0):
        return False

    mcap = _to_float(_get(subject, "buy_market_cap_usd"), 0.0)
    if mcap <= 0:
        mcap = _to_float(_get(subject, "market_cap_usd"), 0.0)
    if not (
        float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_MCAP_USD", 50_000.0) or 50_000.0)
        <= mcap
        <= float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MAX_MCAP_USD", 100_000.0) or 100_000.0)
    ):
        return False

    price5m = _to_float(_get(subject, "buy_price_pct_5m"), None)
    if price5m is None:
        price5m = _to_float(_get(subject, "price_pct_5m"), None)
    if price5m is None or not (
        float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_PRICE5M_PCT", 25.0) or 25.0)
        <= price5m
        <= float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MAX_PRICE5M_PCT", 100.0) or 100.0)
    ):
        return False

    txns_5m = _to_float(_get(subject, "buy_txns_last_5m"), 0.0)
    if txns_5m <= 0:
        txns_5m = _to_float(_get(subject, "txns_last_5m"), 0.0)
    if txns_5m < float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_TXNS_5M", 500) or 500):
        return False

    rank_raw = _get(subject, "rank_score")
    if rank_raw in (None, ""):
        rank_raw = _get(subject, "research_rank_score")
    if rank_raw not in (None, ""):
        rank = _to_float(rank_raw, 0.0)
        if rank < float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_RANK_SCORE", 61.0) or 61.0):
            return False

    return True


def _is_green_sniper_subject(subject: Any) -> bool:
    lane = str(_get(subject, "entry_lane", "") or "").strip().lower()
    profile = str(_get(subject, "gate_profile", "") or _get(subject, "sniper_gate_profile", "") or "").strip().lower()
    bucket = str(_get(subject, "size_bucket", "") or "").strip().lower()
    return (
        lane == "pump_early_green_candle_sniper"
        or profile.startswith("green_sniper")
        or bucket.startswith("green_sniper")
    )


def _is_research_rank_subject(subject: Any) -> bool:
    lane = str(_get(subject, "entry_lane", "") or "").strip().lower()
    profile = str(_get(subject, "gate_profile", "") or _get(subject, "sniper_gate_profile", "") or "").strip().lower()
    tier = str(_get(subject, "profit_lane_tier", "") or "").strip().lower()
    return lane == "pump_early_research_rank_canary" or tier == "pump_early_research_rank_canary" or profile == "research_rank_canary"


def _is_birth_probe_micro_subject(subject: Any) -> bool:
    lane = str(_get(subject, "entry_lane", "") or "").strip().lower()
    profile = str(_get(subject, "gate_profile", "") or _get(subject, "sniper_gate_profile", "") or "").strip().lower()
    tier = str(_get(subject, "profit_lane_tier", "") or "").strip().lower()
    return (
        lane == "pump_early_birth_probe_micro_canary"
        or tier == "pump_early_birth_probe_micro_canary"
        or profile == "birth_probe_micro_canary"
    )


def _is_late_momentum_subject(subject: Any) -> bool:
    lane = str(_get(subject, "entry_lane", "") or "").strip().lower()
    profile = str(_get(subject, "gate_profile", "") or _get(subject, "sniper_gate_profile", "") or "").strip().lower()
    tier = str(_get(subject, "profit_lane_tier", "") or "").strip().lower()
    return lane == "pump_early_late_momentum_watch" or tier == "pump_early_late_momentum_watch" or profile == "late_momentum_watch"


def _is_green_birth_probe_subject(subject: Any) -> bool:
    profile = str(_get(subject, "gate_profile", "") or _get(subject, "sniper_gate_profile", "") or "").strip().lower()
    tier = str(_get(subject, "profit_lane_tier", "") or "").strip().lower()
    return profile == "green_sniper_birth_probe" or tier == "green_sniper_birth_probe"


def _cfg_runner_step(prefix: str, index: int, trigger_default: float, fraction_default: float) -> bird_runner_exit.BirdRunnerStep:
    trigger = _to_float(getattr(CFG, f"{prefix}_TP{index}_PCT", trigger_default), trigger_default)
    fraction = _to_float(getattr(CFG, f"{prefix}_TP{index}_FRACTION", fraction_default), fraction_default)
    return bird_runner_exit.BirdRunnerStep(trigger, fraction)


def _configured_runner_steps(
    prefix: str,
    defaults: tuple[tuple[float, float], ...],
) -> tuple[bird_runner_exit.BirdRunnerStep, ...]:
    steps = tuple(
        _cfg_runner_step(prefix, idx, trigger, fraction)
        for idx, (trigger, fraction) in enumerate(defaults, start=1)
    )
    return tuple(
        sorted(
            (step for step in steps if step.trigger_pct > 0.0 and 0.0 < step.fraction < 1.0),
            key=lambda step: step.trigger_pct,
        )
    )


def _runner_ladder_overrides(
    subject: Any,
) -> tuple[tuple[bird_runner_exit.BirdRunnerStep, ...] | None, float | None]:
    if _is_birth_probe_micro_subject(subject):
        return (
            _configured_runner_steps(
                "BIRTH_PROBE_MICRO_CANARY",
                ((25.0, 0.15), (100.0, 0.20), (300.0, 0.20), (700.0, 0.15)),
            ),
            _to_float(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_MOONBAG_FRACTION", 0.30), 0.30),
        )

    profile = resolve_runner_exit_profile(subject)
    if profile == "jackpot_runner":
        return (
            _configured_runner_steps(
                "PUMP_EARLY_PROFIT_RUNNER_JACKPOT",
                ((100.0, 0.20), (300.0, 0.20), (500.0, 0.15), (1000.0, 0.15)),
            ),
            _to_float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MOONBAG_FRACTION", 0.30), 0.30),
        )

    if profile == "green_sniper_runner" or _is_green_birth_probe_subject(subject) or _is_late_momentum_subject(subject):
        return (
            _configured_runner_steps(
                "GREEN_SNIPER_MOONSHOT",
                ((25.0, 0.15), (100.0, 0.15), (300.0, 0.15), (700.0, 0.15)),
            ),
            _to_float(getattr(CFG, "GREEN_SNIPER_MOONSHOT_MOONBAG_FRACTION", 0.40), 0.40),
        )

    return None, None


def effective_exit_policy(subject: Any) -> ExitPolicy:
    regime = resolve_entry_regime(subject)
    base_lock_floor_pct = max(
        0.0,
        _override_value(regime, "post_partial_lock_floor_pct", float(CFG.POST_PARTIAL_LOCK_FLOOR_PCT)),
    )
    base_max_giveback_pct = max(
        0.0,
        _override_value(regime, "post_partial_max_giveback_pct", float(CFG.POST_PARTIAL_MAX_GIVEBACK_PCT)),
    )
    peak_pct = _to_float(_get(subject, "highest_pnl_pct"), 0.0)
    if peak_pct <= 0:
        peak_pct = _to_float(_get(subject, "max_pnl_pct_seen"), 0.0)
    runner_exit_profile, runner_profile_state, runner_lock_floor_pct, runner_max_giveback_pct = _runner_policy_overrides(
        subject,
        float(peak_pct or 0.0),
    )
    if runner_lock_floor_pct is not None:
        base_lock_floor_pct = max(0.0, float(runner_lock_floor_pct))
    if runner_max_giveback_pct is not None:
        base_max_giveback_pct = max(0.0, float(runner_max_giveback_pct))
    tp_partial_fraction = min(
        max(_override_value(regime, "tp_partial_fraction", float(CFG.TP_PARTIAL_FRACTION)), 0.05),
        0.95,
    )
    if runner_exit_profile:
        runner_partial = _profit_runner_profiles().get(runner_exit_profile, {}).get("partial_fraction")
        if runner_partial is not None:
            tp_partial_fraction = min(max(float(runner_partial), 0.05), 0.95)
    tp_partial_trigger_pct = _override_value(regime, "tp_partial_trigger_pct", float(CFG.TP_PARTIAL_TRIGGER_PCT))
    tp_partial_enabled = bool(CFG.TP_PARTIAL_ENABLED)
    green_sniper_subject = _is_green_sniper_subject(subject)
    if green_sniper_subject:
        tp_partial_enabled = bool(getattr(CFG, "GREEN_SNIPER_TP_PARTIAL_ENABLED", True))
        tp_partial_trigger_pct = float(getattr(CFG, "GREEN_SNIPER_TP_PARTIAL_TRIGGER_PCT", 25.0) or 25.0)
        if bool(getattr(CFG, "GREEN_SNIPER_POST_PARTIAL_PROTECTION_ENABLED", True)) and (
            runner_lock_floor_pct is None or runner_profile_state in {None, "pre_step"}
        ):
            base_lock_floor_pct = float(
                getattr(CFG, "GREEN_SNIPER_POST_PARTIAL_LOCK_FLOOR_PCT", base_lock_floor_pct) or base_lock_floor_pct
            )
            base_max_giveback_pct = float(
                getattr(CFG, "GREEN_SNIPER_POST_PARTIAL_MAX_GIVEBACK_PCT", base_max_giveback_pct)
                or base_max_giveback_pct
            )
    take_profit_pct = _override_value(regime, "take_profit_pct", float(CFG.TAKE_PROFIT_PCT))
    if green_sniper_subject and tp_partial_enabled:
        take_profit_pct = max(float(take_profit_pct), float(tp_partial_trigger_pct))

    dry_run = _effective_dry_run(subject)
    if tp_partial_enabled and bird_runner_exit.bird_runner_multi_partial_enabled(dry_run=dry_run, cfg=CFG):
        steps, _moonbag_fraction = _runner_ladder_overrides(subject)
        if not steps:
            steps = bird_runner_exit.configured_bird_runner_steps(CFG)
        if steps:
            take_profit_pct = max(float(take_profit_pct), float(steps[0].trigger_pct))
    protection_enabled = _override_bool(
        regime,
        "post_partial_protection_enabled",
        bool(CFG.POST_PARTIAL_PROTECTION_ENABLED),
    )
    if protection_enabled:
        protection_enabled = (
            bool(getattr(CFG, "POST_PARTIAL_PROTECTION_PAPER_ENABLED", True))
            if dry_run
            else bool(getattr(CFG, "POST_PARTIAL_PROTECTION_LIVE_ENABLED", False))
        )
    if protection_enabled:
        protection_enabled = bool(getattr(CFG, "POST_PARTIAL_PROTECTION_EXECUTION_ENABLED", True)) and not bool(
            getattr(CFG, "POST_PARTIAL_EXPERIMENT_SHADOW_ONLY", False)
        )
    if not bool(getattr(CFG, "POST_PARTIAL_LOCK_FLOOR_ENABLED", True)):
        base_lock_floor_pct = 0.0

    return ExitPolicy(
        regime=regime,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=_override_value(regime, "stop_loss_pct", float(CFG.STOP_LOSS_PCT)),
        trailing_pct=max(0.0, _override_value(regime, "trailing_pct", float(CFG.TRAILING_PCT))),
        max_holding_h=max(0.0, _override_value(regime, "max_holding_h", float(CFG.MAX_HOLDING_H))),
        max_hard_hold_h=max(0.0, _override_value(regime, "max_hard_hold_h", float(CFG.MAX_HARD_HOLD_H))),
        tp_partial_enabled=tp_partial_enabled,
        tp_partial_trigger_pct=tp_partial_trigger_pct,
        tp_partial_fraction=tp_partial_fraction,
        post_partial_stop_pct=_override_value(regime, "post_partial_stop_pct", float(CFG.POST_PARTIAL_STOP_PCT)),
        post_partial_trailing_pct=max(0.0, _override_value(regime, "post_partial_trailing_pct", float(CFG.POST_PARTIAL_TRAILING_PCT))),
        post_partial_protection_enabled=protection_enabled,
        post_partial_lock_floor_pct=base_lock_floor_pct,
        post_partial_max_giveback_pct=base_max_giveback_pct,
        pre_partial_time_stop_min=max(
            0.0,
            _override_value(regime, "pre_partial_time_stop_min", float(CFG.PRE_PARTIAL_TIME_STOP_MIN)),
        ),
        pre_partial_time_stop_max_pnl_pct=_override_value(
            regime,
            "pre_partial_time_stop_max_pnl_pct",
            float(CFG.PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT),
        ),
        pre_partial_time_stop_min_peak_pct=_override_value(
            regime,
            "pre_partial_time_stop_min_peak_pct",
            float(CFG.PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT),
        ),
        pre_partial_retrace_trigger_pct=max(
            0.0,
            _override_value(regime, "pre_partial_retrace_trigger_pct", float(CFG.PRE_PARTIAL_RETRACE_TRIGGER_PCT)),
        ),
        pre_partial_retrace_giveback_pct=max(
            0.0,
            _override_value(
                regime,
                "pre_partial_retrace_giveback_pct",
                float(CFG.PRE_PARTIAL_RETRACE_GIVEBACK_PCT),
            ),
        ),
        pre_partial_retrace_floor_pct=_override_value(
            regime,
            "pre_partial_retrace_floor_pct",
            float(CFG.PRE_PARTIAL_RETRACE_FLOOR_PCT),
        ),
        early_drop_kill_pct=max(0.0, _override_value(regime, "early_drop_kill_pct", float(CFG.EARLY_DROP_KILL_PCT))),
        early_drop_window_min=max(0.0, _override_value(regime, "early_drop_window_min", float(CFG.EARLY_DROP_WINDOW_MIN))),
        liq_crush_fraction=max(0.0, min(1.0, float(CFG.KILL_LIQ_FRACTION))),
        liq_crush_drop_pct=max(0.0, float(CFG.LIQ_CRUSH_DROP_PCT)),
        liq_crush_window_min=max(0.0, float(CFG.LIQ_CRUSH_WINDOW_MIN)),
        liq_crush_abs_fract=max(0.0, min(1.0, float(CFG.LIQ_CRUSH_ABS_FRACT))),
        no_expansion_max_pct=float(CFG.NO_EXPANSION_MAX_PCT),
        no_pump_window_min=max(0.0, _override_value(regime, "no_pump_window_min", float(CFG.NO_PUMP_WINDOW_MIN))),
        no_pump_min_pnl_pct=_override_value(regime, "no_pump_min_pnl_pct", float(CFG.NO_PUMP_MIN_PNL_PCT)),
        no_pump_max_pnl_pct=_override_optional_value(
            regime,
            "no_pump_max_pnl_pct",
            getattr(CFG, "NO_PUMP_MAX_PNL_PCT", None),
        ),
        time_stop_min=max(0.0, _override_value(regime, "time_stop_min", float(CFG.TIME_STOP_MIN))),
        time_stop_max_pnl_pct=_override_value(regime, "time_stop_max_pnl_pct", float(CFG.TIME_STOP_MAX_PNL_PCT)),
        time_stop_min_peak_pct=_override_value(regime, "time_stop_min_peak_pct", float(CFG.TIME_STOP_MIN_PEAK_PCT)),
        runner_exit_profile=runner_exit_profile,
        runner_profile_state=runner_profile_state,
    )


def should_take_partial(subject: Any, pnl_pct: float) -> bool:
    policy = effective_exit_policy(subject)
    if not policy.tp_partial_enabled:
        return False
    plan = partial_ladder_plan(subject, pnl_pct)
    if bool(plan.get("enabled")):
        return float(plan.get("sell_fraction_of_remaining") or 0.0) > 0.0
    if _to_bool(_get(subject, "partial_taken"), False):
        return False
    return float(pnl_pct) >= float(policy.tp_partial_trigger_pct)


def partial_fraction(subject: Any) -> float:
    return effective_exit_policy(subject).tp_partial_fraction


def _position_qty_state(subject: Any) -> tuple[int, int, int]:
    remaining = int(max(0.0, _to_float(_get(subject, "qty"), 0.0)))
    realized = int(max(0.0, _to_float(_get(subject, "realized_qty"), 0.0)))
    entry = int(max(0.0, _to_float(_get(subject, "entry_qty"), 0.0)))
    if entry <= 0 and (remaining > 0 or realized > 0):
        entry = remaining + realized
    return entry, remaining, realized


def partial_ladder_plan(subject: Any, pnl_pct: float) -> dict[str, Any]:
    entry_qty, remaining_qty, realized_qty = _position_qty_state(subject)
    dry_run = _effective_dry_run(subject)
    enabled = (
        entry_qty > 0
        and remaining_qty > 0
        and bird_runner_exit.bird_runner_multi_partial_enabled(dry_run=dry_run, cfg=CFG)
    )
    if not enabled:
        return {
            "enabled": False,
            "target_secured_fraction": 0.0,
            "already_secured_fraction": 0.0,
            "pending_entry_fraction": 0.0,
            "sell_fraction_of_remaining": 0.0,
            "triggered_steps": [],
        }
    steps, moonbag_fraction = _runner_ladder_overrides(subject)
    plan = bird_runner_exit.pending_partial_plan(
        pnl_pct=float(pnl_pct),
        entry_qty=entry_qty,
        remaining_qty=remaining_qty,
        realized_qty=realized_qty,
        cfg=CFG,
        steps=steps,
        moonbag_fraction=moonbag_fraction,
    )
    plan["enabled"] = True
    return plan


def partial_sell_fraction(subject: Any, pnl_pct: float) -> float:
    policy = effective_exit_policy(subject)
    if not policy.tp_partial_enabled:
        return 0.0
    plan = partial_ladder_plan(subject, pnl_pct)
    if bool(plan.get("enabled")):
        return max(0.0, min(0.95, float(plan.get("sell_fraction_of_remaining") or 0.0)))
    if _to_bool(_get(subject, "partial_taken"), False):
        return 0.0
    if float(pnl_pct) < float(policy.tp_partial_trigger_pct):
        return 0.0
    return max(0.0, min(0.95, float(policy.tp_partial_fraction)))


def runner_giveback_emergency_reason(subject: Any, *, pnl_pct: float, peak: float) -> str | None:
    if not bool(getattr(CFG, "RUNNER_GIVEBACK_CLOSE_REMAINING", True)):
        return None
    if not _to_bool(_get(subject, "partial_taken"), False):
        return None
    if float(pnl_pct) <= 0.0:
        return None
    return bird_runner_exit.runner_giveback_emergency_reason(
        peak_pct=float(peak or 0.0),
        pnl_pct=float(pnl_pct),
        dry_run=_effective_dry_run(subject),
        cfg=CFG,
    )


def _post_partial_exit_reason(
    subject: Any,
    policy: ExitPolicy,
    *,
    pnl_pct: float,
    peak: float,
) -> str | None:
    if not _to_bool(_get(subject, "partial_taken"), False):
        return None

    if (
        policy.post_partial_protection_enabled
        and policy.post_partial_lock_floor_pct > 0
        and policy.post_partial_max_giveback_pct > 0
        and peak >= max(
            float(policy.post_partial_lock_floor_pct),
            float(getattr(CFG, "GREEN_SNIPER_POST_PARTIAL_MIN_PEAK_PCT", policy.post_partial_lock_floor_pct) or policy.post_partial_lock_floor_pct)
            if _is_green_sniper_subject(subject)
            else max(
                float(policy.post_partial_lock_floor_pct),
                float(getattr(CFG, "POST_PARTIAL_MIN_PEAK_PCT", 35.0) or 35.0),
            ),
        )
    ):
        protection_floor = max(
            float(policy.post_partial_lock_floor_pct),
            float(peak) - float(policy.post_partial_max_giveback_pct),
        )
        if float(pnl_pct) <= protection_floor:
            if protection_floor <= float(policy.post_partial_lock_floor_pct):
                return "POST_PARTIAL_STOP"
            return "POST_PARTIAL_TRAILING"
        return None

    if float(pnl_pct) <= float(policy.post_partial_stop_pct):
        return "POST_PARTIAL_STOP"
    if policy.post_partial_trailing_pct > 0 and float(pnl_pct) <= (peak - float(policy.post_partial_trailing_pct)):
        return "POST_PARTIAL_TRAILING"
    return None


def green_sniper_early_dump_reason(
    subject: Any,
    *,
    age_s: float,
    pnl_pct: float,
    peak_pct: float | None = None,
) -> str | None:
    prefix = "RESEARCH_RANK_CANARY" if _is_research_rank_subject(subject) else "GREEN_SNIPER"
    if not bool(getattr(CFG, f"{prefix}_EARLY_DUMP_ENABLED", True)):
        return None
    early_dump_after_s = max(0.0, float(getattr(CFG, f"{prefix}_EARLY_DUMP_AFTER_S", 35) or 35))
    early_dump_pnl = float(getattr(CFG, f"{prefix}_EARLY_DUMP_PNL_PCT", -12.0) or -12.0)
    early_dump_ignore_peak = float(getattr(CFG, f"{prefix}_EARLY_DUMP_IGNORE_IF_PEAK_PCT", 15.0) or 15.0)
    confirm_required = max(1, int(getattr(CFG, f"{prefix}_EARLY_DUMP_CONFIRM_TICKS", 2) or 2))
    confirm_seen = int(_to_float(_get(subject, "early_dump_confirm_ticks", None), confirm_required))
    peak_for_dump = peak_pct
    if peak_for_dump is None:
        peak_for_dump = _to_float(_get(subject, "highest_pnl_pct"), 0.0)
        if peak_for_dump <= 0:
            peak_for_dump = _to_float(_get(subject, "peak_pnl_pct"), 0.0)
    if (
        early_dump_after_s > 0
        and age_s >= early_dump_after_s
        and float(pnl_pct) <= early_dump_pnl
        and float(peak_for_dump or 0.0) < early_dump_ignore_peak
        and confirm_seen >= confirm_required
    ):
        return "EARLY_DUMP_CUT"
    return None


def should_exit(
    subject: Any,
    price_now: float | None,
    now: dt.datetime,
    *,
    liq_now: float | None = None,
    pnl_pct: float | None = None,
) -> str | None:
    policy = effective_exit_policy(subject)

    opened = _get(subject, "opened_at")
    if isinstance(opened, str):
        try:
            opened = dt.datetime.fromisoformat(opened)
        except Exception:
            opened = None
    if isinstance(opened, dt.datetime) and opened.tzinfo is None:
        opened = opened.replace(tzinfo=dt.timezone.utc)
    if not isinstance(opened, dt.datetime):
        opened = now if now.tzinfo is not None else now.replace(tzinfo=dt.timezone.utc)

    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)

    age_s = max(0.0, (now - opened).total_seconds())
    age_h = age_s / 3600.0
    age_min = age_s / 60.0

    if price_now is None:
        if age_h >= float(policy.max_holding_h):
            return "TIMEOUT_NOPRICE"
        return None

    buy_price_usd = _to_float(_get(subject, "buy_price_usd"))
    if pnl_pct is None and buy_price_usd > 0:
        pnl_pct = (float(price_now) - buy_price_usd) / buy_price_usd * 100.0

    peak = _to_float(_get(subject, "highest_pnl_pct"), 0.0)
    if peak <= 0:
        peak = _to_float(_get(subject, "peak_pnl_pct"), 0.0)
    partial_taken = _to_bool(_get(subject, "partial_taken"), False)

    if partial_taken and pnl_pct is not None:
        giveback_emergency = runner_giveback_emergency_reason(subject, pnl_pct=float(pnl_pct), peak=peak)
        if giveback_emergency is not None:
            return giveback_emergency
        post_partial_reason = _post_partial_exit_reason(subject, policy, pnl_pct=float(pnl_pct), peak=peak)
        if post_partial_reason is not None:
            return post_partial_reason

    if pnl_pct is not None and _is_birth_probe_micro_subject(subject):
        no_expansion_min = max(
            0.0,
            _to_float(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_EXIT_MIN", 2.0), 2.0),
        )
        expansion_min_pnl = _to_float(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_MIN_PNL", 5.0), 5.0)
        if no_expansion_min > 0 and age_min >= no_expansion_min and peak < expansion_min_pnl and float(pnl_pct) <= expansion_min_pnl:
            return "BIRTH_PROBE_NO_EXPANSION"
        time_stop_min = max(0.0, _to_float(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_TIME_STOP_MIN", 3.0), 3.0))
        if time_stop_min > 0 and age_min >= time_stop_min:
            return "BIRTH_PROBE_TIME_STOP"

    if pnl_pct is not None and (_is_pumpswap_profit_subject(subject) or _is_green_sniper_subject(subject) or _is_research_rank_subject(subject)):
        if _is_green_sniper_subject(subject) or _is_research_rank_subject(subject):
            peak_for_dump = _to_float(_get(subject, "highest_pnl_pct"), 0.0)
            if peak_for_dump <= 0:
                peak_for_dump = _to_float(_get(subject, "peak_pnl_pct"), 0.0)
            early_dump_reason = green_sniper_early_dump_reason(subject, age_s=age_s, pnl_pct=float(pnl_pct), peak_pct=peak_for_dump)
            if early_dump_reason:
                return early_dump_reason
            adverse_after_s = max(0.0, float(getattr(CFG, "GREEN_SNIPER_ADVERSE_TICK_AFTER_S", 45) or 45))
            adverse_pnl = float(getattr(CFG, "GREEN_SNIPER_ADVERSE_TICK_PNL_PCT", -10.0) or -10.0)
        else:
            adverse_after_s = max(0.0, float(getattr(CFG, "PUMP_EARLY_PROFIT_ADVERSE_TICK_AFTER_S", 75) or 75))
            adverse_pnl = float(getattr(CFG, "PUMP_EARLY_PROFIT_ADVERSE_TICK_PNL_PCT", -8.0) or -8.0)
        if adverse_after_s > 0 and age_s >= adverse_after_s and float(pnl_pct) <= adverse_pnl:
            return "ADVERSE_TICK"

    if buy_price_usd > 0 and policy.early_drop_kill_pct > 0 and policy.early_drop_window_min > 0:
        if age_min <= float(policy.early_drop_window_min):
            drop_pct = (buy_price_usd - float(price_now)) / buy_price_usd * 100.0
            if drop_pct >= float(policy.early_drop_kill_pct):
                return "EARLY_DROP"

    entry_liq = _to_float(_get(subject, "buy_liquidity_usd"), 0.0)
    if entry_liq <= 0:
        entry_liq = _to_float(_get(subject, "liq_at_buy_usd"), 0.0)
    if entry_liq > 0 and liq_now and liq_now > 0 and policy.liq_crush_window_min >= 0:
        window_ok = policy.liq_crush_window_min <= 0 or age_min <= float(policy.liq_crush_window_min)
        if window_ok:
            if policy.liq_crush_fraction > 0 and float(liq_now) <= entry_liq * float(policy.liq_crush_fraction):
                return "LIQUIDITY_CRUSH"
            drop_frac = (entry_liq - float(liq_now)) / entry_liq
            if policy.liq_crush_drop_pct > 0 and drop_frac >= float(policy.liq_crush_drop_pct) / 100.0:
                return "LIQUIDITY_CRUSH"
            min_liq = _to_float(getattr(CFG, "MIN_LIQUIDITY_USD", 0.0), 0.0)
            if min_liq > 0 and float(liq_now) < min_liq * float(policy.liq_crush_abs_fract):
                return "LIQUIDITY_CRUSH"

    if pnl_pct is None:
        if age_h >= float(policy.max_holding_h):
            return "TIMEOUT"
        return None

    if policy.no_expansion_max_pct and age_s >= 3600.0 and float(pnl_pct) <= float(policy.no_expansion_max_pct):
        return "NO_EXPANSION"

    if _is_pumpswap_profit_subject(subject) or _is_green_sniper_subject(subject):
        if _is_green_sniper_subject(subject):
            profit_no_pump_window = max(0.0, float(getattr(CFG, "GREEN_SNIPER_NO_PUMP_WINDOW_MIN", 2.0) or 2.0))
            profit_no_pump_peak = float(getattr(CFG, "GREEN_SNIPER_NO_PUMP_MIN_PEAK_PCT", 8.0) or 8.0)
            profit_no_pump_pnl = float(getattr(CFG, "GREEN_SNIPER_NO_PUMP_MAX_PNL_PCT", 0.0) or 0.0)
        else:
            profit_no_pump_window = max(
                0.0,
                float(getattr(CFG, "PUMP_EARLY_PROFIT_NO_PUMP_WINDOW_MIN", 3.0) or 3.0),
            )
            profit_no_pump_peak = float(getattr(CFG, "PUMP_EARLY_PROFIT_NO_PUMP_MIN_PEAK_PCT", 2.0) or 2.0)
            profit_no_pump_pnl = float(getattr(CFG, "PUMP_EARLY_PROFIT_NO_PUMP_MAX_PNL_PCT", 0.0) or 0.0)
        if profit_no_pump_window > 0 and age_min >= profit_no_pump_window:
            if peak < profit_no_pump_peak and float(pnl_pct) <= profit_no_pump_pnl:
                return "NO_PUMP_EXIT"

    if policy.no_pump_window_min > 0 and age_min >= float(policy.no_pump_window_min):
        pnl_ok = policy.no_pump_max_pnl_pct is None or float(pnl_pct) <= float(policy.no_pump_max_pnl_pct)
        if peak < float(policy.no_pump_min_pnl_pct) and pnl_ok:
            return "NO_PUMP_EXIT"

    if policy.time_stop_min > 0 and age_min >= float(policy.time_stop_min):
        if peak < float(policy.time_stop_min_peak_pct) and float(pnl_pct) <= float(policy.time_stop_max_pnl_pct):
            return "TIME_STOP"

    if not partial_taken:
        if policy.pre_partial_retrace_trigger_pct > 0 and policy.pre_partial_retrace_giveback_pct > 0:
            if peak >= float(policy.pre_partial_retrace_trigger_pct):
                retrace_floor = max(
                    float(policy.pre_partial_retrace_floor_pct),
                    float(peak) - float(policy.pre_partial_retrace_giveback_pct),
                )
                if float(pnl_pct) <= retrace_floor:
                    return "PRE_PARTIAL_RETRACE"
        if policy.pre_partial_time_stop_min > 0 and age_min >= float(policy.pre_partial_time_stop_min):
            if peak < float(policy.pre_partial_time_stop_min_peak_pct) and float(pnl_pct) <= float(
                policy.pre_partial_time_stop_max_pnl_pct
            ):
                return "PRE_PARTIAL_TIME_STOP"

    if partial_taken:
        giveback_emergency = runner_giveback_emergency_reason(subject, pnl_pct=float(pnl_pct), peak=peak)
        if giveback_emergency is not None:
            return giveback_emergency
        post_partial_reason = _post_partial_exit_reason(subject, policy, pnl_pct=float(pnl_pct), peak=peak)
        if post_partial_reason is not None:
            return post_partial_reason

    if float(pnl_pct) <= -abs(float(policy.stop_loss_pct)):
        return "STOP_LOSS"

    if float(policy.trailing_pct) > 0 and float(pnl_pct) <= (peak - float(policy.trailing_pct)):
        return "TRAILING_STOP"

    green_waiting_for_partial = _is_green_sniper_subject(subject) and not partial_taken and policy.tp_partial_enabled
    if not green_waiting_for_partial and float(pnl_pct) >= float(policy.take_profit_pct):
        if not (policy.tp_partial_enabled and partial_taken):
            return "TAKE_PROFIT"

    if age_h >= float(policy.max_holding_h):
        if policy.max_hard_hold_h > float(policy.max_holding_h) and float(pnl_pct) >= float(policy.trailing_pct):
            if age_h >= float(policy.max_hard_hold_h):
                return "TIMEOUT_HARD"
            return None
        return "TIMEOUT"

    return None


def describe_exit_policy() -> dict[str, Any]:
    effective_by_regime = {
        regime: effective_exit_policy({"entry_regime": regime}).__dict__
        for regime in ("pump_early", "dex_mature", "revival")
    }
    return {
        "exit_profile_by_regime": bool(CFG.EXIT_PROFILE_BY_REGIME),
        "tp_partial_enabled": bool(CFG.TP_PARTIAL_ENABLED),
        "tp_partial_trigger_pct": float(CFG.TP_PARTIAL_TRIGGER_PCT),
        "tp_partial_fraction": float(CFG.TP_PARTIAL_FRACTION),
        "post_partial_stop_pct": float(CFG.POST_PARTIAL_STOP_PCT),
        "post_partial_trailing_pct": float(CFG.POST_PARTIAL_TRAILING_PCT),
        "post_partial_protection_enabled": bool(CFG.POST_PARTIAL_PROTECTION_ENABLED),
        "post_partial_protection_paper_enabled": bool(getattr(CFG, "POST_PARTIAL_PROTECTION_PAPER_ENABLED", True)),
        "post_partial_protection_live_enabled": bool(getattr(CFG, "POST_PARTIAL_PROTECTION_LIVE_ENABLED", False)),
        "post_partial_protection_execution_enabled": bool(
            getattr(CFG, "POST_PARTIAL_PROTECTION_EXECUTION_ENABLED", True)
        ),
        "post_partial_experiment_shadow_only": bool(getattr(CFG, "POST_PARTIAL_EXPERIMENT_SHADOW_ONLY", False)),
        "post_partial_lock_floor_enabled": bool(getattr(CFG, "POST_PARTIAL_LOCK_FLOOR_ENABLED", True)),
        "post_partial_lock_floor_pct": float(CFG.POST_PARTIAL_LOCK_FLOOR_PCT),
        "post_partial_max_giveback_pct": float(CFG.POST_PARTIAL_MAX_GIVEBACK_PCT),
        "post_partial_min_peak_pct": float(getattr(CFG, "POST_PARTIAL_MIN_PEAK_PCT", 35.0) or 35.0),
        "pre_partial_time_stop_min": float(CFG.PRE_PARTIAL_TIME_STOP_MIN),
        "pre_partial_time_stop_max_pnl_pct": float(CFG.PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT),
        "pre_partial_time_stop_min_peak_pct": float(CFG.PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT),
        "pre_partial_retrace_trigger_pct": float(CFG.PRE_PARTIAL_RETRACE_TRIGGER_PCT),
        "pre_partial_retrace_giveback_pct": float(CFG.PRE_PARTIAL_RETRACE_GIVEBACK_PCT),
        "pre_partial_retrace_floor_pct": float(CFG.PRE_PARTIAL_RETRACE_FLOOR_PCT),
        "no_pump_window_min": float(CFG.NO_PUMP_WINDOW_MIN),
        "no_pump_min_pnl_pct": float(CFG.NO_PUMP_MIN_PNL_PCT),
        "no_pump_max_pnl_pct": CFG.NO_PUMP_MAX_PNL_PCT,
        "time_stop_min": float(CFG.TIME_STOP_MIN),
        "time_stop_max_pnl_pct": float(CFG.TIME_STOP_MAX_PNL_PCT),
        "time_stop_min_peak_pct": float(CFG.TIME_STOP_MIN_PEAK_PCT),
        "regime_overrides_active": {
            regime: any(v is not None for v in overrides.values())
            for regime, overrides in _REGIME_EXIT_OVERRIDES.items()
        },
        "effective_by_regime": effective_by_regime,
        "profit_lane_overrides": {
            "adverse_tick_after_s": int(getattr(CFG, "PUMP_EARLY_PROFIT_ADVERSE_TICK_AFTER_S", 75) or 75),
            "adverse_tick_pnl_pct": float(getattr(CFG, "PUMP_EARLY_PROFIT_ADVERSE_TICK_PNL_PCT", -8.0) or -8.0),
            "no_pump_window_min": float(getattr(CFG, "PUMP_EARLY_PROFIT_NO_PUMP_WINDOW_MIN", 3.0) or 3.0),
            "no_pump_min_peak_pct": float(getattr(CFG, "PUMP_EARLY_PROFIT_NO_PUMP_MIN_PEAK_PCT", 2.0) or 2.0),
            "no_pump_max_pnl_pct": float(getattr(CFG, "PUMP_EARLY_PROFIT_NO_PUMP_MAX_PNL_PCT", 0.0) or 0.0),
        },
        "profit_lane_runner_profiles": _profit_runner_profiles(),
        "bird_runner_multi_partial": {
            "enabled": bool(getattr(CFG, "BIRD_RUNNER_MULTI_PARTIAL_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED", False)),
            "steps": [step.__dict__ for step in bird_runner_exit.configured_bird_runner_steps(CFG)],
            "moonbag_fraction": float(getattr(CFG, "BIRD_MOONBAG_FRACTION", 0.15) or 0.15),
            "jackpot_steps": [
                step.__dict__
                for step in _configured_runner_steps(
                    "PUMP_EARLY_PROFIT_RUNNER_JACKPOT",
                    ((100.0, 0.20), (300.0, 0.20), (500.0, 0.15), (1000.0, 0.15)),
                )
            ],
            "jackpot_moonbag_fraction": float(
                getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MOONBAG_FRACTION", 0.30) or 0.30
            ),
            "green_moonshot_steps": [
                step.__dict__
                for step in _configured_runner_steps(
                    "GREEN_SNIPER_MOONSHOT",
                    ((25.0, 0.15), (100.0, 0.15), (300.0, 0.15), (700.0, 0.15)),
                )
            ],
            "green_moonshot_moonbag_fraction": float(
                getattr(CFG, "GREEN_SNIPER_MOONSHOT_MOONBAG_FRACTION", 0.40) or 0.40
            ),
        },
        "runner_giveback_emergency": {
            "enabled": bool(getattr(CFG, "RUNNER_GIVEBACK_EMERGENCY_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "RUNNER_GIVEBACK_EMERGENCY_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED", False)),
            "peak_100_max_giveback": float(getattr(CFG, "RUNNER_GIVEBACK_PEAK_100_MAX_GIVEBACK", 25.0) or 25.0),
            "peak_300_max_giveback": float(getattr(CFG, "RUNNER_GIVEBACK_PEAK_300_MAX_GIVEBACK", 60.0) or 60.0),
            "peak_700_max_giveback": float(getattr(CFG, "RUNNER_GIVEBACK_PEAK_700_MAX_GIVEBACK", 120.0) or 120.0),
            "peak_1000_max_giveback": float(getattr(CFG, "RUNNER_GIVEBACK_PEAK_1000_MAX_GIVEBACK", 220.0) or 220.0),
            "peak_2000_max_giveback": float(getattr(CFG, "RUNNER_GIVEBACK_PEAK_2000_MAX_GIVEBACK", 450.0) or 450.0),
            "close_remaining": bool(getattr(CFG, "RUNNER_GIVEBACK_CLOSE_REMAINING", True)),
        },
        "green_sniper_runner_profile": _profit_runner_profiles()["green_sniper_runner"],
        "green_sniper_post_partial_protection": {
            "enabled": bool(getattr(CFG, "GREEN_SNIPER_POST_PARTIAL_PROTECTION_ENABLED", True)),
            "lock_floor_pct": float(getattr(CFG, "GREEN_SNIPER_POST_PARTIAL_LOCK_FLOOR_PCT", 20.0) or 20.0),
            "max_giveback_pct": float(getattr(CFG, "GREEN_SNIPER_POST_PARTIAL_MAX_GIVEBACK_PCT", 5.0) or 5.0),
            "min_peak_pct": float(getattr(CFG, "GREEN_SNIPER_POST_PARTIAL_MIN_PEAK_PCT", 35.0) or 35.0),
        },
        "early_dump_cut": {
            "green_sniper": {
                "enabled": bool(getattr(CFG, "GREEN_SNIPER_EARLY_DUMP_ENABLED", True)),
                "after_s": int(getattr(CFG, "GREEN_SNIPER_EARLY_DUMP_AFTER_S", 35) or 35),
                "pnl_pct": float(getattr(CFG, "GREEN_SNIPER_EARLY_DUMP_PNL_PCT", -12.0) or -12.0),
                "confirm_ticks": int(getattr(CFG, "GREEN_SNIPER_EARLY_DUMP_CONFIRM_TICKS", 2) or 2),
                "ignore_if_peak_pct": float(getattr(CFG, "GREEN_SNIPER_EARLY_DUMP_IGNORE_IF_PEAK_PCT", 15.0) or 15.0),
            },
            "research_rank_canary": {
                "enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_EARLY_DUMP_ENABLED", True)),
                "after_s": int(getattr(CFG, "RESEARCH_RANK_CANARY_EARLY_DUMP_AFTER_S", 35) or 35),
                "pnl_pct": float(getattr(CFG, "RESEARCH_RANK_CANARY_EARLY_DUMP_PNL_PCT", -12.0) or -12.0),
                "confirm_ticks": int(getattr(CFG, "RESEARCH_RANK_CANARY_EARLY_DUMP_CONFIRM_TICKS", 2) or 2),
                "ignore_if_peak_pct": float(
                    getattr(CFG, "RESEARCH_RANK_CANARY_EARLY_DUMP_IGNORE_IF_PEAK_PCT", 15.0) or 15.0
                ),
            },
        },
    }


def write_post_partial_activation_audit(path: Any | None = None) -> dict[str, Any]:
    target = path or (PROJECT_ROOT / "data" / "metrics" / "post_partial_activation_audit.json")
    effective_dry_run = _effective_dry_run()
    effective_by_regime = {
        regime: effective_exit_policy({"entry_regime": regime}).__dict__
        for regime in ("pump_early", "dex_mature", "revival")
    }
    active_regimes = [
        regime
        for regime, policy in effective_by_regime.items()
        if bool(policy.get("post_partial_protection_enabled"))
    ]
    experiment_enabled = bool(getattr(CFG, "POST_PARTIAL_EXPERIMENT_ENABLED", True))
    experiment_mode = str(getattr(CFG, "POST_PARTIAL_EXPERIMENT_MODE", "paper_shadow") or "paper_shadow").strip().lower()
    shadow_only = bool(getattr(CFG, "POST_PARTIAL_EXPERIMENT_SHADOW_ONLY", False))
    execution_enabled = bool(getattr(CFG, "POST_PARTIAL_PROTECTION_EXECUTION_ENABLED", True))
    surface = "paper_exit_policy" if effective_dry_run else "live_exit_policy"
    execution_changed = bool(effective_dry_run and execution_enabled and active_regimes and not shadow_only)
    payload = {
        "updated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "surface": surface,
        "active": bool(active_regimes),
        "execution_changed": execution_changed,
        "shadow_only": shadow_only,
        "cfg_dry_run": bool(getattr(CFG, "DRY_RUN", True)),
        "runtime_dry_run_override": _RUNTIME_DRY_RUN_OVERRIDE,
        "effective_dry_run": bool(effective_dry_run),
        "post_partial_protection_enabled": bool(getattr(CFG, "POST_PARTIAL_PROTECTION_ENABLED", True)),
        "post_partial_protection_paper_enabled": bool(
            getattr(CFG, "POST_PARTIAL_PROTECTION_PAPER_ENABLED", True)
        ),
        "post_partial_protection_live_enabled": bool(
            getattr(CFG, "POST_PARTIAL_PROTECTION_LIVE_ENABLED", False)
        ),
        "post_partial_protection_execution_enabled": execution_enabled,
        "post_partial_experiment_shadow_only": shadow_only,
        "post_partial_lock_floor_enabled": bool(getattr(CFG, "POST_PARTIAL_LOCK_FLOOR_ENABLED", True)),
        "post_partial_lock_floor_pct": float(getattr(CFG, "POST_PARTIAL_LOCK_FLOOR_PCT", 0.0) or 0.0),
        "post_partial_max_giveback_pct": float(getattr(CFG, "POST_PARTIAL_MAX_GIVEBACK_PCT", 0.0) or 0.0),
        "runtime_surface": surface,
        "runtime_protection_active": bool(active_regimes),
        "active_regimes": active_regimes,
        "experiment_shadow_enabled": bool(experiment_enabled and experiment_mode == "paper_shadow"),
        "experiment_shadow_only": bool(shadow_only or (experiment_enabled and experiment_mode == "paper_shadow" and not active_regimes)),
        "effective_by_regime": effective_by_regime,
    }
    target = PROJECT_ROOT / target if isinstance(target, str) else target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return payload


__all__ = [
    "ExitPolicy",
    "describe_exit_policy",
    "effective_exit_policy",
    "partial_ladder_plan",
    "partial_fraction",
    "partial_sell_fraction",
    "green_sniper_early_dump_reason",
    "resolve_runner_exit_profile",
    "resolve_entry_regime",
    "runner_giveback_emergency_reason",
    "set_runtime_dry_run",
    "should_exit",
    "should_take_partial",
    "write_post_partial_activation_audit",
]
