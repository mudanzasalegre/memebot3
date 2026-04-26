from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Any

from config.config import CFG


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
    _ = subject
    # Research/aggressive buys are not validated productively; keep the runner small and defensive.
    return "broad_runner"


def resolve_runner_exit_profile(subject: Any) -> str | None:
    explicit = str(_get(subject, "runner_exit_profile", "") or "").strip().lower()
    if explicit in {"broad_runner", "prime_runner", "meteor_runner"}:
        return explicit
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

    return ExitPolicy(
        regime=regime,
        take_profit_pct=_override_value(regime, "take_profit_pct", float(CFG.TAKE_PROFIT_PCT)),
        stop_loss_pct=_override_value(regime, "stop_loss_pct", float(CFG.STOP_LOSS_PCT)),
        trailing_pct=max(0.0, _override_value(regime, "trailing_pct", float(CFG.TRAILING_PCT))),
        max_holding_h=max(0.0, _override_value(regime, "max_holding_h", float(CFG.MAX_HOLDING_H))),
        max_hard_hold_h=max(0.0, _override_value(regime, "max_hard_hold_h", float(CFG.MAX_HARD_HOLD_H))),
        tp_partial_enabled=bool(CFG.TP_PARTIAL_ENABLED),
        tp_partial_trigger_pct=_override_value(regime, "tp_partial_trigger_pct", float(CFG.TP_PARTIAL_TRIGGER_PCT)),
        tp_partial_fraction=tp_partial_fraction,
        post_partial_stop_pct=_override_value(regime, "post_partial_stop_pct", float(CFG.POST_PARTIAL_STOP_PCT)),
        post_partial_trailing_pct=max(0.0, _override_value(regime, "post_partial_trailing_pct", float(CFG.POST_PARTIAL_TRAILING_PCT))),
        post_partial_protection_enabled=_override_bool(
            regime,
            "post_partial_protection_enabled",
            bool(CFG.POST_PARTIAL_PROTECTION_ENABLED),
        ),
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
    if _to_bool(_get(subject, "partial_taken"), False):
        return False
    return float(pnl_pct) >= float(policy.tp_partial_trigger_pct)


def partial_fraction(subject: Any) -> float:
    return effective_exit_policy(subject).tp_partial_fraction


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

    if pnl_pct is not None and _is_pumpswap_profit_subject(subject):
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

    peak = _to_float(_get(subject, "highest_pnl_pct"), 0.0)
    if peak <= 0:
        peak = _to_float(_get(subject, "peak_pnl_pct"), 0.0)

    if _is_pumpswap_profit_subject(subject):
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

    partial_taken = _to_bool(_get(subject, "partial_taken"), False)
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
        if (
            policy.post_partial_protection_enabled
            and policy.post_partial_lock_floor_pct > 0
            and policy.post_partial_max_giveback_pct > 0
            and peak >= float(policy.post_partial_lock_floor_pct)
        ):
            protection_floor = max(
                float(policy.post_partial_lock_floor_pct),
                float(peak) - float(policy.post_partial_max_giveback_pct),
            )
            if float(pnl_pct) <= protection_floor:
                if protection_floor <= float(policy.post_partial_lock_floor_pct):
                    return "POST_PARTIAL_STOP"
                return "POST_PARTIAL_TRAILING"
        else:
            if float(pnl_pct) <= float(policy.post_partial_stop_pct):
                return "POST_PARTIAL_STOP"
            if policy.post_partial_trailing_pct > 0 and float(pnl_pct) <= (peak - float(policy.post_partial_trailing_pct)):
                return "POST_PARTIAL_TRAILING"

    if float(pnl_pct) <= -abs(float(policy.stop_loss_pct)):
        return "STOP_LOSS"

    if float(policy.trailing_pct) > 0 and float(pnl_pct) <= (peak - float(policy.trailing_pct)):
        return "TRAILING_STOP"

    if float(pnl_pct) >= float(policy.take_profit_pct):
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
        "post_partial_lock_floor_pct": float(CFG.POST_PARTIAL_LOCK_FLOOR_PCT),
        "post_partial_max_giveback_pct": float(CFG.POST_PARTIAL_MAX_GIVEBACK_PCT),
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
    }


__all__ = [
    "ExitPolicy",
    "describe_exit_policy",
    "effective_exit_policy",
    "partial_fraction",
    "resolve_runner_exit_profile",
    "resolve_entry_regime",
    "should_exit",
    "should_take_partial",
]
