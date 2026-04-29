from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd

from config.config import CFG, DB_URI, PROJECT_ROOT
from trade_pnl import total_pnl_pct_from_record
from utils.runtime_telemetry import RUNTIME_EVENTS_PATH


_CONFIG_KEYS = [
    "DRY_RUN",
    "TRADE_AMOUNT_SOL",
    "MIN_BUY_SOL",
    "AI_THRESHOLD",
    "BUY_SOFT_SCORE_MIN",
    "MIN_AGE_MIN",
    "MIN_HOLDERS",
    "MIN_LIQUIDITY_USD",
    "MIN_VOL_USD_24H",
    "MIN_MARKET_CAP_USD",
    "MAX_MARKET_CAP_USD",
    "FILTER_PROFILE_BY_DISCOVERY",
    "SNAPSHOT_QUALITY_FILTER_ENABLED",
    "SNAPSHOT_MAX_MISSING_FIELDS",
    "SNAPSHOT_REQUIRE_ACTIVITY_SIGNAL",
    "SNAPSHOT_REQUIRE_SOCIAL_OR_TREND",
    "SNAPSHOT_REQUIRE_RUG_SCORE",
    "SNAPSHOT_ALLOWED_PRICE_SOURCES",
    "MAX_ACTIVE_POSITIONS",
    "REGIME_PUMP_EARLY_MAX_AGE_MIN",
    "DYNAMIC_SIZING_ENABLED",
    "AI_SIZING_ENABLED",
    "PUMP_EARLY_EXECUTION_MODE",
    "DEX_MATURE_EXECUTION_MODE",
    "REVIVAL_EXECUTION_MODE",
    "PAPER_AGGRESSIVE_TRADING_ENABLED",
    "PAPER_AGGRESSIVE_CONFIRM_SNAPSHOTS",
    "PAPER_AGGRESSIVE_CONFIRM_BACKOFF_S",
    "PAPER_AGGRESSIVE_MIN_AGE_MIN",
    "PAPER_AGGRESSIVE_MIN_LIQUIDITY_USD",
    "PAPER_AGGRESSIVE_MIN_MARKET_CAP_USD",
    "PAPER_AGGRESSIVE_MAX_MARKET_CAP_USD",
    "PAPER_AGGRESSIVE_MIN_SCORE_TOTAL",
    "PAPER_AGGRESSIVE_MIN_RANK_SCORE",
    "PAPER_AGGRESSIVE_MIN_TXNS_5M",
    "PAPER_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS",
    "PAPER_AGGRESSIVE_MAX_PRICE_IMPACT_PCT",
    "PAPER_AGGRESSIVE_REQUIRE_ROUTE",
    "PAPER_AGGRESSIVE_REQUIRE_PRICE",
    "PAPER_AGGRESSIVE_BUY_RESEARCH_LANES",
    "LIVE_AGGRESSIVE_TRADING_ENABLED",
    "LIVE_AGGRESSIVE_CONFIRM_SNAPSHOTS",
    "LIVE_AGGRESSIVE_CONFIRM_BACKOFF_S",
    "LIVE_AGGRESSIVE_MIN_AGE_MIN",
    "LIVE_AGGRESSIVE_MIN_LIQUIDITY_USD",
    "LIVE_AGGRESSIVE_MIN_MARKET_CAP_USD",
    "LIVE_AGGRESSIVE_MAX_MARKET_CAP_USD",
    "LIVE_AGGRESSIVE_MIN_SCORE_TOTAL",
    "LIVE_AGGRESSIVE_MIN_RANK_SCORE",
    "LIVE_AGGRESSIVE_MIN_TXNS_5M",
    "LIVE_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS",
    "LIVE_AGGRESSIVE_MAX_PRICE_IMPACT_PCT",
    "LIVE_AGGRESSIVE_REQUIRE_ROUTE",
    "LIVE_AGGRESSIVE_REQUIRE_PRICE",
    "LIVE_AGGRESSIVE_BUY_RESEARCH_LANES",
    "LIVE_AGGRESSIVE_CONTINUE_ON_HEALTH",
    "LIVE_AGGRESSIVE_HEALTH_SIZE_CAP_MULTIPLIER",
    "PUMP_EARLY_QUALITY_MIN_POINTS",
    "PUMP_EARLY_QUALITY_MIN_AGE_MIN",
    "PUMP_EARLY_QUALITY_MIN_LIQUIDITY_USD",
    "PUMP_EARLY_QUALITY_MIN_VOLUME_USD_24H",
    "PUMP_EARLY_QUALITY_MIN_MARKET_CAP_USD",
    "PUMP_EARLY_QUALITY_MIN_HOLDERS",
    "PUMP_EARLY_QUALITY_MIN_SCORE_TOTAL",
    "PUMP_EARLY_QUALITY_MAX_PRICE_IMPACT_PCT",
    "SIZE_MIN_MULTIPLIER",
    "SIZE_MID_MULTIPLIER",
    "SIZE_MAX_MULTIPLIER",
    "PUMP_EARLY_LIVE_HARD_MAX_MARKET_CAP_USD",
    "PUMP_EARLY_LIVE_HARD_MAX_PRICE_IMPACT_PCT",
    "PUMP_EARLY_LIVE_MAX_SNAPSHOT_MISSING_FIELDS",
    "PUMP_EARLY_SNIPER_ENABLED",
    "PUMP_EARLY_SNIPER_MODE",
    "PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD",
    "PUMP_EARLY_SNIPER_MIN_MARKET_CAP_USD",
    "PUMP_EARLY_SNIPER_MIN_SCORE_TOTAL",
    "PUMP_EARLY_SNIPER_MIN_RANK_SCORE",
    "PUMP_EARLY_SNIPER_MIN_TXNS_5M",
    "PUMP_EARLY_SNIPER_MIN_PRICE_PCT_5M",
    "PUMP_EARLY_SNIPER_MAX_PRICE_PCT_5M",
    "PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT",
    "PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS",
    "PUMP_EARLY_SNIPER_PAPER_CONTINUE_ON_HEALTH",
    "PUMP_EARLY_SNIPER_PAPER_RECOVERY_SIZE_CAP",
    "PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_ENABLED",
    "GREEN_SNIPER_ALLOW_PROXY_LIQUIDITY_PAPER",
    "GREEN_SNIPER_RANK_GUARD_ENABLED",
    "GREEN_SNIPER_RANK_GUARD_MIN_SCORE",
    "GREEN_SNIPER_RANK_GUARD_BYPASS_PAPER_BIRTH_PROBE",
    "GREEN_SNIPER_PAPER_BIRTH_PROBE_ENABLED",
    "GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_AGE_MIN",
    "GREEN_SNIPER_PAPER_BIRTH_PROBE_MIN_LIQUIDITY_USD",
    "GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_PRICE_IMPACT_PCT",
    "PUMP_EARLY_PROFIT_LANE_ENABLED",
    "PUMP_EARLY_PROFIT_DEX_ALLOWLIST",
    "PUMP_EARLY_PROFIT_REQUIRE_REAL_LIQUIDITY",
    "PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD",
    "PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL",
    "PUMP_EARLY_PROFIT_MIN_AGE_MIN",
    "PUMP_EARLY_PROFIT_MAX_AGE_MIN",
    "PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT",
    "PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD",
    "PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD",
    "PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES",
    "PUMP_EARLY_METEOR_PRIME_ENABLED",
    "PUMP_EARLY_METEOR_PRIME_MIN_LIQUIDITY_USD",
    "PUMP_EARLY_METEOR_PRIME_MAX_LIQUIDITY_USD",
    "PUMP_EARLY_METEOR_PRIME_MIN_MARKET_CAP_USD",
    "PUMP_EARLY_METEOR_PRIME_MAX_MARKET_CAP_USD",
    "PUMP_EARLY_METEOR_PRIME_MIN_PRICE_PCT_5M",
    "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_PCT_5M",
    "PUMP_EARLY_METEOR_PRIME_MIN_TXNS_5M",
    "PUMP_EARLY_METEOR_PRIME_MIN_SCORE_TOTAL",
    "PUMP_EARLY_METEOR_PRIME_MIN_AGE_MIN",
    "PUMP_EARLY_METEOR_PRIME_MAX_AGE_MIN",
    "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_IMPACT_PCT",
    "PUMP_EARLY_METEOR_PRIME_MIN_VOLUME_USD_24H",
    "PUMP_EARLY_BREAKOUT_PROBE_ENABLED",
    "PUMP_EARLY_BREAKOUT_MIN_LIQUIDITY_USD",
    "PUMP_EARLY_BREAKOUT_MAX_LIQUIDITY_USD",
    "PUMP_EARLY_BREAKOUT_MIN_MARKET_CAP_USD",
    "PUMP_EARLY_BREAKOUT_MAX_MARKET_CAP_USD",
    "PUMP_EARLY_BREAKOUT_MIN_PRICE_PCT_5M",
    "PUMP_EARLY_BREAKOUT_MAX_PRICE_PCT_5M",
    "PUMP_EARLY_BREAKOUT_MIN_TXNS_5M",
    "PUMP_EARLY_BREAKOUT_MIN_VOLUME_USD_24H",
    "PUMP_EARLY_BREAKOUT_MIN_SCORE_TOTAL",
    "PUMP_EARLY_BREAKOUT_MIN_RANK_SCORE",
    "PUMP_EARLY_BREAKOUT_MIN_AGE_MIN",
    "PUMP_EARLY_BREAKOUT_MAX_AGE_MIN",
    "PUMP_EARLY_BREAKOUT_MAX_PRICE_IMPACT_PCT",
    "PUMP_EARLY_BREAKOUT_MAX_OPEN_PAPER",
    "PUMP_EARLY_BREAKOUT_MAX_OPEN_LIVE_CANARY",
    "PUMP_EARLY_BREAKOUT_HEALTH_ISOLATED",
    "PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED",
    "PUMP_EARLY_PROFIT_HEALTH_REBASE_CURRENT_GATE",
    "PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD",
    "PUMP_EARLY_PROFIT_DEEP_NEG_PRICE5M_PCT",
    "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_TXNS_5M",
    "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_VOLUME_USD_24H",
    "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_PCT",
    "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_MIN_MCAP_USD",
    "PUMP_EARLY_PROFIT_DEAD_VOLUME_MIN_USD_24H",
    "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_USD_24H",
    "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_TXNS_5M",
    "PUMP_EARLY_PROFIT_HOT_PRICE5M_MIN_PCT",
    "PUMP_EARLY_PROFIT_HOT_PRICE5M_MAX_PCT",
    "PUMP_EARLY_PROFIT_HOT_MCAP_MIN_USD",
    "PUMP_EARLY_PROFIT_HOT_MIN_LIQUIDITY_USD",
    "PUMP_EARLY_PROFIT_HOT_MIN_TXNS_5M",
    "PUMP_EARLY_PROFIT_HOT_MIN_VOLUME_USD_24H",
    "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H",
    "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_TXNS_5M",
    "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_PRICE5M_PCT",
    "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_TXNS_5M",
    "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_VOLUME_USD_24H",
    "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT",
    "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT",
    "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD",
    "PUMP_EARLY_PROFIT_PNL_GUARD_ENABLED",
    "PUMP_EARLY_PROFIT_PNL_GUARD_JACKPOT_PRICE5M_MIN",
    "PUMP_EARLY_PROFIT_PNL_GUARD_50K_100K_WEAK_PRICE5M_MAX",
    "PUMP_EARLY_PROFIT_PNL_GUARD_50K_100K_WEAK_MIN_TXNS_5M",
    "PUMP_EARLY_PROFIT_PNL_GUARD_LOCAL_TOP_MIN_MCAP_USD",
    "PUMP_EARLY_PROFIT_PNL_GUARD_MID_MOMENTUM_MIN_MCAP_USD",
    "PUMP_EARLY_PROFIT_MAX_OPEN_PAPER",
    "PUMP_EARLY_PROFIT_MAX_OPEN_LIVE_CANARY",
    "PUMP_EARLY_AGGRESSIVE_RESEARCH_GUARD_ENABLED",
    "PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PRICE5M_RANGES",
    "PUMP_EARLY_AGGRESSIVE_RESEARCH_DEX_ALLOWLIST",
    "PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_HIGH_MCAP_USD",
    "PUMP_EARLY_AGGRESSIVE_RESEARCH_HIGH_MCAP_ALLOW_MIN_TXNS_5M",
    "PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PROXY",
    "PUMP_EARLY_AGGRESSIVE_RESEARCH_HOT_PRICE5M_MIN_PCT",
    "PUMP_EARLY_AGGRESSIVE_RESEARCH_HOT_MIN_TXNS_5M",
    "PUMP_EARLY_PROFIT_RUNNER_BROAD_LOCK_FLOOR_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_BROAD_PARTIAL_FRACTION",
    "PUMP_EARLY_PROFIT_RUNNER_BROAD_MAX_GIVEBACK_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_LOCK_FLOOR_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_PRIME_PARTIAL_FRACTION",
    "PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_MAX_GIVEBACK_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_PEAK_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_LOCK_FLOOR_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_MAX_GIVEBACK_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_LOCK_FLOOR_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_METEOR_PARTIAL_FRACTION",
    "PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_MAX_GIVEBACK_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_PEAK_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_LOCK_FLOOR_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_MAX_GIVEBACK_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP2_PEAK_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP2_LOCK_FLOOR_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_METEOR_MOMENTUM_PRICE5M_PCT",
    "PUMP_EARLY_PROFIT_RUNNER_METEOR_MOMENTUM_MIN_TXNS_5M",
    "PUMP_EARLY_PROFIT_RECOVERY_RECENT_TRADES",
    "PUMP_EARLY_PROFIT_RECOVERY_RECENT_MIN_AVG_PNL_PCT",
    "PUMP_EARLY_PROFIT_RECOVERY_RECENT_MAX_CONSECUTIVE_LOSSES",
    "PUMP_EARLY_RESEARCH_ALLOW_PROXY",
    "PAPER_PNL_STRICT_HEALTH",
    "PUMP_EARLY_PROFIT_ADVERSE_TICK_AFTER_S",
    "PUMP_EARLY_PROFIT_ADVERSE_TICK_PNL_PCT",
    "PUMP_EARLY_PROFIT_NO_PUMP_WINDOW_MIN",
    "PUMP_EARLY_PROFIT_NO_PUMP_MIN_PEAK_PCT",
    "PUMP_EARLY_PROFIT_NO_PUMP_MAX_PNL_PCT",
    "RESEARCH_SHADOW_MAX_OPEN",
    "RESEARCH_SHADOW_MAX_OPEN_PER_REGIME",
    "LIVE_RANK_SCORE_FALLBACK_MIN",
    "SIZE_ACCEPTABLE_MIN_POINTS",
    "SIZE_PREMIUM_MIN_POINTS",
    "PUMP_EARLY_MAX_SIZE_MULTIPLIER",
    "DEX_MATURE_MAX_SIZE_MULTIPLIER",
    "REVIVAL_MAX_SIZE_MULTIPLIER",
    "MAX_ACTIVE_POSITIONS_PER_REGIME",
    "PUMP_EARLY_MAX_ACTIVE_POSITIONS",
    "DEX_MATURE_MAX_ACTIVE_POSITIONS",
    "REVIVAL_MAX_ACTIVE_POSITIONS",
    "REQUIRE_JUPITER_FOR_BUY",
    "EXIT_PROFILE_BY_REGIME",
    "TP_PARTIAL_ENABLED",
    "TP_PARTIAL_TRIGGER_PCT",
    "TP_PARTIAL_FRACTION",
    "POST_PARTIAL_STOP_PCT",
    "POST_PARTIAL_TRAILING_PCT",
    "POST_PARTIAL_PROTECTION_ENABLED",
    "POST_PARTIAL_LOCK_FLOOR_PCT",
    "POST_PARTIAL_MAX_GIVEBACK_PCT",
    "PRE_PARTIAL_TIME_STOP_MIN",
    "PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT",
    "PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT",
    "PRE_PARTIAL_RETRACE_TRIGGER_PCT",
    "PRE_PARTIAL_RETRACE_GIVEBACK_PCT",
    "PRE_PARTIAL_RETRACE_FLOOR_PCT",
    "NO_PUMP_WINDOW_MIN",
    "NO_PUMP_MIN_PNL_PCT",
    "NO_PUMP_MAX_PNL_PCT",
    "TIME_STOP_MIN",
    "TIME_STOP_MAX_PNL_PCT",
    "TIME_STOP_MIN_PEAK_PCT",
    "PUMP_EARLY_TRAILING_PCT",
    "PUMP_EARLY_MAX_HOLDING_H",
    "PUMP_EARLY_TP_PARTIAL_TRIGGER_PCT",
    "PUMP_EARLY_TP_PARTIAL_FRACTION",
    "PUMP_EARLY_POST_PARTIAL_STOP_PCT",
    "PUMP_EARLY_POST_PARTIAL_TRAILING_PCT",
    "PUMP_EARLY_POST_PARTIAL_PROTECTION_ENABLED",
    "PUMP_EARLY_POST_PARTIAL_LOCK_FLOOR_PCT",
    "PUMP_EARLY_POST_PARTIAL_MAX_GIVEBACK_PCT",
    "PUMP_EARLY_PRE_PARTIAL_TIME_STOP_MIN",
    "PUMP_EARLY_PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT",
    "PUMP_EARLY_PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT",
    "PUMP_EARLY_PRE_PARTIAL_RETRACE_TRIGGER_PCT",
    "PUMP_EARLY_PRE_PARTIAL_RETRACE_GIVEBACK_PCT",
    "PUMP_EARLY_PRE_PARTIAL_RETRACE_FLOOR_PCT",
    "PUMP_EARLY_NO_PUMP_WINDOW_MIN",
    "PUMP_EARLY_NO_PUMP_MIN_PNL_PCT",
    "PUMP_EARLY_NO_PUMP_MAX_PNL_PCT",
    "PUMP_EARLY_TIME_STOP_MIN",
    "PUMP_EARLY_TIME_STOP_MAX_PNL_PCT",
    "PUMP_EARLY_TIME_STOP_MIN_PEAK_PCT",
    "DEX_MATURE_TRAILING_PCT",
    "DEX_MATURE_MAX_HOLDING_H",
    "DEX_MATURE_TP_PARTIAL_TRIGGER_PCT",
    "DEX_MATURE_TP_PARTIAL_FRACTION",
    "DEX_MATURE_POST_PARTIAL_STOP_PCT",
    "DEX_MATURE_POST_PARTIAL_TRAILING_PCT",
    "DEX_MATURE_POST_PARTIAL_PROTECTION_ENABLED",
    "DEX_MATURE_POST_PARTIAL_LOCK_FLOOR_PCT",
    "DEX_MATURE_POST_PARTIAL_MAX_GIVEBACK_PCT",
    "DEX_MATURE_PRE_PARTIAL_TIME_STOP_MIN",
    "DEX_MATURE_PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT",
    "DEX_MATURE_PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT",
    "DEX_MATURE_PRE_PARTIAL_RETRACE_TRIGGER_PCT",
    "DEX_MATURE_PRE_PARTIAL_RETRACE_GIVEBACK_PCT",
    "DEX_MATURE_PRE_PARTIAL_RETRACE_FLOOR_PCT",
    "DEX_MATURE_NO_PUMP_WINDOW_MIN",
    "DEX_MATURE_NO_PUMP_MIN_PNL_PCT",
    "DEX_MATURE_NO_PUMP_MAX_PNL_PCT",
    "DEX_MATURE_TIME_STOP_MIN",
    "DEX_MATURE_TIME_STOP_MAX_PNL_PCT",
    "DEX_MATURE_TIME_STOP_MIN_PEAK_PCT",
    "REVIVAL_TRAILING_PCT",
    "REVIVAL_MAX_HOLDING_H",
    "REVIVAL_TP_PARTIAL_TRIGGER_PCT",
    "REVIVAL_TP_PARTIAL_FRACTION",
    "REVIVAL_POST_PARTIAL_STOP_PCT",
    "REVIVAL_POST_PARTIAL_TRAILING_PCT",
    "REVIVAL_POST_PARTIAL_PROTECTION_ENABLED",
    "REVIVAL_POST_PARTIAL_LOCK_FLOOR_PCT",
    "REVIVAL_POST_PARTIAL_MAX_GIVEBACK_PCT",
    "REVIVAL_PRE_PARTIAL_TIME_STOP_MIN",
    "REVIVAL_PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT",
    "REVIVAL_PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT",
    "REVIVAL_PRE_PARTIAL_RETRACE_TRIGGER_PCT",
    "REVIVAL_PRE_PARTIAL_RETRACE_GIVEBACK_PCT",
    "REVIVAL_PRE_PARTIAL_RETRACE_FLOOR_PCT",
    "REVIVAL_NO_PUMP_WINDOW_MIN",
    "REVIVAL_NO_PUMP_MIN_PNL_PCT",
    "REVIVAL_NO_PUMP_MAX_PNL_PCT",
    "REVIVAL_TIME_STOP_MIN",
    "REVIVAL_TIME_STOP_MAX_PNL_PCT",
    "REVIVAL_TIME_STOP_MIN_PEAK_PCT",
    "DISCOVERY_INTERVAL",
    "VALIDATION_BATCH_SIZE",
    "TRAIN_FORWARD_HOLDOUT_DAYS",
    "TRAIN_FORWARD_HOLDOUT_PCT",
    "TRAINING_WINDOW_DAYS",
    "MIN_THRESHOLD_CHANGE",
    "PRECISION_AT_K_PCT",
    "ML_GATE_MODE",
    "ML_LIVE_PROFIT_MODE",
    "ML_RESEARCH_MODE",
    "ML_UNKNOWN_LANE_MODE",
    "ML_ALLOW_RESEARCH_LIVE",
    "ML_ALLOW_UNKNOWN_LIVE",
    "ML_SIZING_ENABLED",
    "ML_RISK_MODEL_ENABLED",
    "ML_RISK_VETO_ENABLED",
    "ML_EV_MODEL_ENABLED",
    "ML_REJECT_SHADOW_ENABLED",
    "ML_RETRAIN_IN_MAIN_LOOP",
    "ML_TRAINING_DAEMON_ENABLED",
    "ML_DRIFT_MONITOR_ENABLED",
    "ML_AUTO_PROMOTE_LANES",
    "LIVE_MAX_DAILY_BUYS",
    "LIVE_MAX_DAILY_LOSS_SOL",
    "LIVE_MAX_CONSECUTIVE_LOSSES",
    "ML_MIN_DATASET_ROWS",
    "ML_MIN_POSITIVES",
    "ML_MIN_UNIQUE_TOKENS",
    "ML_MIN_REALIZED_RETURN_ROWS",
    "ML_MIN_HOLDOUT_ROWS",
    "ML_MIN_HOLDOUT_POSITIVES",
    "ML_MIN_NON_CONSTANT_FEATURES",
    "ML_TUNE_OBJECTIVE",
    "ML_TUNE_PRECISION_FLOOR",
    "ML_TUNE_MIN_SELECTED",
    "ML_TUNE_MIN_REALIZED_SELECTED",
    "ML_SELECTION_MIN_DELTA",
    "ML_TRAIN_ENTRY_LANE_ALLOWLIST",
    "ML_TRAIN_DEX_ALLOWLIST",
    "TAKE_PROFIT_PCT",
    "STOP_LOSS_PCT",
    "TRAILING_PCT",
    "WIN_PCT",
    "ML_POSITIVE_PNL_PCT",
    "SOCIALS_ENABLED",
    "SOCIALS_ASYNC_ONLY",
    "SOCIALS_HOT_PATH_BLOCKING",
    "SOCIALS_TIMEOUT_S",
    "SOCIALS_CACHE_TTL_S",
    "SOCIALS_MAX_CONCURRENT",
    "SOCIALS_SUSPICIOUS_ENABLED",
    "GREEN_SNIPER_REQUIRE_SOCIALS",
    "GREEN_SNIPER_SOCIALS_BONUS_ENABLED",
    "GREEN_SNIPER_SOCIALS_SCORE_BONUS",
    "GREEN_SNIPER_SOCIALS_RISK_PENALTY",
    "GREEN_SNIPER_SOCIALS_CAN_INCREASE_SIZE_PAPER",
    "GREEN_SNIPER_SOCIALS_CAN_INCREASE_SIZE_LIVE",
    "GREEN_SNIPER_SOCIALS_CAN_DECREASE_SIZE",
    "GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_BLOCK",
    "GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_REDUCE_SIZE",
]


def _round(value: Any, digits: int = 3) -> Any:
    try:
        if value is None or pd.isna(value):
            return None
        return round(float(value), digits)
    except Exception:
        return value


def snapshot_effective_config() -> Dict[str, Any]:
    return {key: getattr(CFG, key, None) for key in _CONFIG_KEYS}


def _db_path_from_uri(db_uri: str) -> Path:
    if db_uri.startswith("sqlite+aiosqlite:///"):
        return Path(db_uri.replace("sqlite+aiosqlite:///", "", 1))
    return Path(DB_URI)


def load_positions_frame(db_path: Path | None = None) -> pd.DataFrame:
    path = db_path or _db_path_from_uri(DB_URI)
    if not Path(path).exists():
        return pd.DataFrame()

    with sqlite3.connect(path) as conn:
        tables = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('positions', 'position')",
            conn,
        )
        if tables.empty:
            return pd.DataFrame()
        table_name = "positions" if "positions" in set(tables["name"]) else "position"
        return pd.read_sql_query(f"SELECT * FROM {table_name}", conn)


def _compute_pnl_series(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype="float64")
    return df.apply(total_pnl_pct_from_record, axis=1)


def summarize_positions(df: pd.DataFrame) -> Dict[str, Any]:
    if df.empty:
        return {
            "rows": 0,
            "closed_rows": 0,
            "open_rows": 0,
            "win_rate_pct": None,
            "avg_pnl_pct": None,
            "median_pnl_pct": None,
            "avg_hold_minutes": None,
            "avg_giveback_pct": None,
            "simple_max_drawdown_pct_points": None,
            "exit_breakdown": [],
            "partial_breakdown": [],
        }

    frame = df.copy()
    frame["opened_at"] = pd.to_datetime(frame.get("opened_at"), utc=True, errors="coerce")
    frame["closed_at"] = pd.to_datetime(frame.get("closed_at"), utc=True, errors="coerce")
    frame["computed_total_pnl_pct"] = _compute_pnl_series(frame)

    closed = frame[frame.get("closed", 0).fillna(0).astype(int) == 1].copy()
    open_rows = int(len(frame) - len(closed))
    if not closed.empty:
        closed["hold_minutes"] = (closed["closed_at"] - closed["opened_at"]).dt.total_seconds() / 60.0
        closed["giveback_pct"] = closed.get("highest_pnl_pct", pd.Series(dtype="float64")).fillna(0.0) - closed["computed_total_pnl_pct"]
        equity = closed.sort_values("closed_at")["computed_total_pnl_pct"].fillna(0.0).cumsum()
        peak = equity.cummax()
        drawdown = equity - peak
        simple_max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
        win_rate = float((closed["computed_total_pnl_pct"] > 0).mean() * 100.0)
    else:
        simple_max_drawdown = 0.0
        win_rate = None

    exit_breakdown: List[Dict[str, Any]] = []
    if not closed.empty and "exit_reason" in closed:
        for reason, group in closed.groupby(closed["exit_reason"].fillna("UNKNOWN")):
            exit_breakdown.append(
                {
                    "exit_reason": str(reason),
                    "count": int(len(group)),
                    "avg_pnl_pct": _round(group["computed_total_pnl_pct"].mean(), 3),
                    "median_pnl_pct": _round(group["computed_total_pnl_pct"].median(), 3),
                    "avg_giveback_pct": _round(group["giveback_pct"].mean(), 3),
                }
            )
        exit_breakdown.sort(key=lambda row: (-int(row["count"]), str(row["exit_reason"])))

    partial_breakdown: List[Dict[str, Any]] = []
    if not closed.empty and "partial_taken" in closed:
        for partial_taken, group in closed.groupby(closed["partial_taken"].fillna(0).astype(int)):
            partial_breakdown.append(
                {
                    "partial_taken": bool(partial_taken),
                    "count": int(len(group)),
                    "avg_pnl_pct": _round(group["computed_total_pnl_pct"].mean(), 3),
                    "median_pnl_pct": _round(group["computed_total_pnl_pct"].median(), 3),
                }
            )
        partial_breakdown.sort(key=lambda row: (not bool(row["partial_taken"])))

    return {
        "rows": int(len(frame)),
        "closed_rows": int(len(closed)),
        "open_rows": open_rows,
        "win_rate_pct": _round(win_rate, 3),
        "avg_pnl_pct": _round(closed["computed_total_pnl_pct"].mean(), 3) if not closed.empty else None,
        "median_pnl_pct": _round(closed["computed_total_pnl_pct"].median(), 3) if not closed.empty else None,
        "avg_hold_minutes": _round(closed["hold_minutes"].mean(), 3) if not closed.empty else None,
        "avg_giveback_pct": _round(closed["giveback_pct"].mean(), 3) if not closed.empty else None,
        "simple_max_drawdown_pct_points": _round(simple_max_drawdown, 3),
        "exit_breakdown": exit_breakdown,
        "partial_breakdown": partial_breakdown,
    }


def _parquet_files(features_dir: Path) -> Iterable[Path]:
    return sorted(features_dir.glob("features_*.parquet"))


def summarize_features(features_dir: Path | None = None) -> Dict[str, Any]:
    base_dir = Path(features_dir or CFG.FEATURES_DIR)
    files = list(_parquet_files(base_dir))
    if not files:
        return {
            "files": 0,
            "rows": 0,
            "positives": 0,
            "unique_tokens": 0,
            "constant_columns": [],
            "null_pct": {},
        }

    df = pd.concat((pd.read_parquet(path) for path in files), ignore_index=True)
    constant_columns = [
        col for col in df.columns
        if df[col].dropna().nunique() <= 1
    ]
    null_pct = {
        col: _round(df[col].isna().mean() * 100.0, 3)
        for col in df.columns
    }

    return {
        "files": len(files),
        "rows": int(len(df)),
        "positives": int(df["label"].fillna(0).astype(int).sum()) if "label" in df.columns else 0,
        "unique_tokens": int(df["address"].nunique()) if "address" in df.columns else 0,
        "constant_columns": constant_columns,
        "null_pct": null_pct,
    }


def build_baseline_snapshot(
    *,
    db_path: Path | None = None,
    features_dir: Path | None = None,
) -> Dict[str, Any]:
    positions_df = load_positions_frame(db_path=db_path)
    return {
        "project_root": str(PROJECT_ROOT),
        "config": snapshot_effective_config(),
        "positions": summarize_positions(positions_df),
        "features": summarize_features(features_dir=features_dir),
    }


def render_baseline_markdown(snapshot: Dict[str, Any]) -> str:
    config = snapshot["config"]
    positions = snapshot["positions"]
    features = snapshot["features"]

    lines = [
        "# Baseline",
        "",
        f"- Project root: `{snapshot['project_root']}`",
        "",
        "## Config efectiva",
        "",
    ]
    for key, value in config.items():
        lines.append(f"- `{key}`: `{value}`")

    lines.extend(
        [
            "",
            "## DB de posiciones",
            "",
            f"- Filas totales: `{positions['rows']}`",
            f"- Cerradas: `{positions['closed_rows']}`",
            f"- Abiertas: `{positions['open_rows']}`",
            f"- Win rate simple: `{positions['win_rate_pct']}`",
            f"- PnL medio (%): `{positions['avg_pnl_pct']}`",
            f"- PnL mediano (%): `{positions['median_pnl_pct']}`",
            f"- Hold medio (min): `{positions['avg_hold_minutes']}`",
            f"- Giveback medio (%): `{positions['avg_giveback_pct']}`",
            f"- Max drawdown simple (p.p.): `{positions['simple_max_drawdown_pct_points']}`",
            "",
            "### Breakdown por exit_reason",
            "",
        ]
    )

    if positions["exit_breakdown"]:
        for row in positions["exit_breakdown"]:
            lines.append(
                f"- `{row['exit_reason']}`: count=`{row['count']}`, avg_pnl=`{row['avg_pnl_pct']}`, median_pnl=`{row['median_pnl_pct']}`, avg_giveback=`{row['avg_giveback_pct']}`"
            )
    else:
        lines.append("- Sin datos")

    lines.extend(["", "### Breakdown por parcial", ""])
    if positions["partial_breakdown"]:
        for row in positions["partial_breakdown"]:
            lines.append(
                f"- `partial_taken={row['partial_taken']}`: count=`{row['count']}`, avg_pnl=`{row['avg_pnl_pct']}`, median_pnl=`{row['median_pnl_pct']}`"
            )
    else:
        lines.append("- Sin datos")

    lines.extend(
        [
            "",
            "## Dataset",
            "",
            f"- Ficheros parquet: `{features['files']}`",
            f"- Filas: `{features['rows']}`",
            f"- Positivos: `{features['positives']}`",
            f"- Tokens unicos: `{features['unique_tokens']}`",
            f"- Columnas constantes: `{', '.join(features['constant_columns']) if features['constant_columns'] else '(ninguna)'}`",
            "",
            "### Nulos por columna (%)",
            "",
        ]
    )

    for col, pct in sorted(features["null_pct"].items()):
        lines.append(f"- `{col}`: `{pct}`")

    lines.append("")
    return "\n".join(lines)


def load_tokens_frame(db_path: Path | None = None) -> pd.DataFrame:
    path = db_path or _db_path_from_uri(DB_URI)
    if not Path(path).exists():
        return pd.DataFrame()

    with sqlite3.connect(path) as conn:
        tables = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tokens'",
            conn,
        )
        if tables.empty:
            return pd.DataFrame()
        return pd.read_sql_query("SELECT * FROM tokens", conn)


def load_feature_snapshots(features_dir: Path | None = None) -> pd.DataFrame:
    base_dir = Path(features_dir or CFG.FEATURES_DIR)
    files = list(_parquet_files(base_dir))
    if not files:
        return pd.DataFrame()

    df = pd.concat((pd.read_parquet(path) for path in files), ignore_index=True)
    if "address" not in df.columns:
        return pd.DataFrame()

    if "ts" in df.columns:
        snap_ts = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    elif "timestamp" in df.columns:
        snap_ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    else:
        snap_ts = pd.Series(pd.NaT, index=df.index)

    frame = df.copy()
    frame["_snapshot_ts"] = snap_ts
    frame = frame.sort_values(["address", "_snapshot_ts"], kind="mergesort")
    frame = frame.drop_duplicates(subset=["address"], keep="first")
    return frame.drop(columns=["_snapshot_ts"], errors="ignore")


def load_runtime_events(events_path: Path | None = None) -> pd.DataFrame:
    path = Path(events_path or RUNTIME_EVENTS_PATH)
    if not path.exists():
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            rows.append(json.loads(raw))
        except Exception:
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    if "ts_utc" in df.columns:
        df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True, errors="coerce")
    if "first_seen_epoch_s" in df.columns:
        df["first_seen_utc"] = pd.to_datetime(df["first_seen_epoch_s"], unit="s", utc=True, errors="coerce")
    return df


def _coalesce(frame: pd.DataFrame, *columns: str) -> pd.Series:
    result = pd.Series(pd.NA, index=frame.index, dtype="object")
    for col in columns:
        if col in frame.columns:
            series = frame[col]
            mask = result.isna() & series.notna()
            if mask.any():
                result.loc[mask] = series.loc[mask]
    return result


def _bucket_numeric(values: pd.Series, edges: list[float], labels: list[str]) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    bucketed = pd.cut(
        numeric,
        bins=edges,
        labels=labels,
        include_lowest=True,
        right=False,
    )
    return bucketed.astype("string").fillna("unknown")


def _group_trade_stats(frame: pd.DataFrame, column: str) -> list[dict[str, Any]]:
    if frame.empty or column not in frame.columns:
        return []

    group_values = frame[column].astype("string").fillna("unknown")
    tmp = frame.assign(_group=group_values)

    rows: list[dict[str, Any]] = []
    for group_name, group in tmp.groupby("_group", dropna=False):
        pnl = pd.to_numeric(group["computed_total_pnl_pct"], errors="coerce")
        giveback = pd.to_numeric(group.get("giveback_pct"), errors="coerce")
        hold = pd.to_numeric(group.get("hold_minutes"), errors="coerce")
        rows.append(
            {
                "group": str(group_name),
                "count": int(len(group)),
                "win_rate_pct": _round((pnl > 0).mean() * 100.0, 3),
                "avg_pnl_pct": _round(pnl.mean(), 3),
                "median_pnl_pct": _round(pnl.median(), 3),
                "sum_pnl_pct_points": _round(pnl.sum(), 3),
                "avg_giveback_pct": _round(giveback.mean(), 3),
                "avg_hold_minutes": _round(hold.mean(), 3),
            }
        )

    rows.sort(key=lambda row: (-int(row["count"]), str(row["group"])))
    return rows


def _presence_from_snapshot(frame: pd.DataFrame, value_col: str, missing_col: str | None = None) -> pd.Series:
    if missing_col and missing_col in frame.columns:
        return pd.to_numeric(frame[missing_col], errors="coerce").fillna(1).eq(0)
    if value_col in frame.columns:
        return frame[value_col].notna()
    return pd.Series(False, index=frame.index)


def _build_trade_context(
    *,
    db_path: Path | None = None,
    features_dir: Path | None = None,
) -> pd.DataFrame:
    positions = load_positions_frame(db_path=db_path)
    if positions.empty:
        return pd.DataFrame()

    frame = positions.copy()
    frame["opened_at"] = pd.to_datetime(frame.get("opened_at"), utc=True, errors="coerce")
    frame["closed_at"] = pd.to_datetime(frame.get("closed_at"), utc=True, errors="coerce")
    frame["computed_total_pnl_pct"] = _compute_pnl_series(frame)
    frame = frame[frame.get("closed", 0).fillna(0).astype(int) == 1].copy()
    if frame.empty:
        return frame

    frame["hold_minutes"] = (frame["closed_at"] - frame["opened_at"]).dt.total_seconds() / 60.0
    frame["giveback_pct"] = pd.to_numeric(frame.get("highest_pnl_pct"), errors="coerce").fillna(0.0) - pd.to_numeric(
        frame["computed_total_pnl_pct"], errors="coerce"
    ).fillna(0.0)

    tokens = load_tokens_frame(db_path=db_path)
    if not tokens.empty:
        tokens = tokens.rename(
            columns={
                "address": "token_address",
                "created_at": "token_created_at",
                "holders": "token_holders",
                "rug_score": "token_rug_score",
                "social_ok": "token_social_ok",
                "trend": "token_trend",
                "score_total": "token_score_total",
                "discovered_via": "token_discovered_via",
                "discovered_at": "token_discovered_at",
                "dex_id": "token_dex_id",
            }
        )
        keep = [
            col
            for col in (
                "token_address",
                "token_created_at",
                "token_holders",
                "token_rug_score",
                "token_social_ok",
                "token_trend",
                "token_score_total",
                "token_discovered_via",
                "token_discovered_at",
                "token_dex_id",
            )
            if col in tokens.columns
        ]
        frame = frame.merge(tokens[keep], left_on="address", right_on="token_address", how="left")

    snapshots = load_feature_snapshots(features_dir=features_dir)
    if not snapshots.empty:
        rename_map = {
            "timestamp": "feat_timestamp",
            "ts": "feat_ts",
            "txns_last_5m": "feat_txns_last_5m",
            "txns_last_5m_buys": "feat_txns_last_5m_buys",
            "txns_last_5m_sells": "feat_txns_last_5m_sells",
            "holders": "feat_holders",
            "rug_score": "feat_rug_score",
            "social_ok": "feat_social_ok",
            "social_status": "feat_social_status",
            "social_link_count": "feat_social_link_count",
            "social_risk_flags": "feat_social_risk_flags",
            "trend": "feat_trend",
            "score_total": "feat_score_total",
            "missing_liquidity": "feat_missing_liquidity",
            "missing_volume": "feat_missing_volume",
            "missing_holders": "feat_missing_holders",
            "missing_rug_score": "feat_missing_rug_score",
            "missing_socials": "feat_missing_socials",
            "missing_trend": "feat_missing_trend",
        }
        keep = ["address"] + [col for col in rename_map if col in snapshots.columns]
        snapshots = snapshots[keep].rename(columns=rename_map)
        frame = frame.merge(snapshots, on="address", how="left")

    if "token_created_at" in frame.columns:
        frame["token_created_at"] = pd.to_datetime(frame["token_created_at"], utc=True, errors="coerce")
        frame["age_at_buy_minutes"] = (
            frame["opened_at"] - frame["token_created_at"]
        ).dt.total_seconds() / 60.0
    else:
        frame["age_at_buy_minutes"] = pd.Series(pd.NA, index=frame.index)

    frame["report_discovered_via"] = _coalesce(frame, "token_discovered_via").astype("string").fillna("unknown")
    frame["report_entry_regime"] = _coalesce(frame, "entry_regime").astype("string")
    fallback_regime = frame["report_discovered_via"].replace(
        {
            "pumpfun": "pump_early",
            "revival": "revival",
            "dex": "dex_mature",
        }
    )
    frame["report_entry_regime"] = frame["report_entry_regime"].fillna(fallback_regime).fillna("unknown")
    frame["report_entry_lane"] = _coalesce(frame, "entry_lane").astype("string").fillna("unknown")
    frame["report_gate_profile"] = _coalesce(frame, "gate_profile").astype("string").fillna("unknown")
    frame["report_dex_id"] = _coalesce(frame, "buy_dex_id", "token_dex_id").astype("string").fillna("unknown")
    frame["report_buy_dex_id"] = _coalesce(frame, "buy_dex_id", "token_dex_id").astype("string").fillna("unknown")
    proxy_series = pd.to_numeric(
        frame.get("buy_liquidity_is_proxy", pd.Series(pd.NA, index=frame.index)),
        errors="coerce",
    )
    frame["liquidity_proxy_bucket"] = proxy_series.map({0.0: "real", 1.0: "proxy"}).astype("string").fillna("unknown")
    frame["report_runner_exit_profile"] = _coalesce(frame, "runner_exit_profile", "exit_profile").astype("string").fillna("unknown")
    frame["report_social_status"] = _coalesce(frame, "feat_social_status", "token_social_ok").astype("string").fillna("unknown")
    frame["report_social_status"] = frame["report_social_status"].replace({"True": "present", "False": "missing", "1": "present", "0": "missing"})
    frame["report_score_total"] = pd.to_numeric(_coalesce(frame, "token_score_total", "feat_score_total"), errors="coerce")
    frame["report_size_bucket"] = _coalesce(frame, "size_bucket").astype("string").fillna("unknown")
    frame["report_size_multiplier"] = pd.to_numeric(_coalesce(frame, "size_multiplier"), errors="coerce")
    frame["report_buy_amount_sol"] = pd.to_numeric(_coalesce(frame, "buy_amount_sol"), errors="coerce")
    frame["size_multiplier_bucket"] = _bucket_numeric(
        frame["report_size_multiplier"],
        [0.0, 0.3, 0.6, 0.9, float("inf")],
        ["0-0.3x", "0.3-0.6x", "0.6-0.9x", "0.9x+"],
    )
    frame["buy_amount_bucket"] = _bucket_numeric(
        frame["report_buy_amount_sol"],
        [0.0, 0.03, 0.06, 0.10, 0.20, float("inf")],
        ["0-0.03", "0.03-0.06", "0.06-0.10", "0.10-0.20", "0.20+"],
    )
    frame["age_bucket"] = _bucket_numeric(
        frame["age_at_buy_minutes"],
        [0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0, float("inf")],
        ["0-0.5m", "0.5-1m", "1-2m", "2-3m", "3-5m", "5-10m", "10-30m", "30m+"],
    )
    frame["liquidity_bucket"] = _bucket_numeric(
        frame.get("buy_liquidity_usd", pd.Series(pd.NA, index=frame.index)),
        [0.0, 2_000.0, 5_000.0, 10_000.0, 25_000.0, float("inf")],
        ["0-2k", "2k-5k", "5k-10k", "10k-25k", "25k+"],
    )
    frame["market_cap_bucket"] = _bucket_numeric(
        frame.get("buy_market_cap_usd", pd.Series(pd.NA, index=frame.index)),
        [0.0, 10_000.0, 25_000.0, 50_000.0, 100_000.0, 250_000.0, float("inf")],
        ["0-10k", "10k-25k", "25k-50k", "50k-100k", "100k-250k", "250k+"],
    )
    frame["price5m_bucket"] = _bucket_numeric(
        frame.get("buy_price_pct_5m", pd.Series(pd.NA, index=frame.index)),
        [-float("inf"), 0.0, 25.0, 50.0, 100.0, 180.0, 300.0, float("inf")],
        ["<0", "0-25", "25-50", "50-100", "100-180", "180-300", "300+"],
    )
    frame["txns5m_bucket"] = _bucket_numeric(
        frame.get("buy_txns_last_5m", pd.Series(pd.NA, index=frame.index)),
        [0.0, 35.0, 80.0, 150.0, 300.0, 600.0, float("inf")],
        ["0-34", "35-79", "80-149", "150-299", "300-599", "600+"],
    )
    frame["score_bucket"] = _bucket_numeric(
        frame["report_score_total"],
        [0.0, 20.0, 40.0, 60.0, 80.0, float("inf")],
        ["0-19", "20-39", "40-59", "60-79", "80+"],
    )
    frame["price_source_pair"] = (
        frame.get("price_source_at_buy", pd.Series(pd.NA, index=frame.index)).astype("string").fillna("unknown")
        + " -> "
        + frame.get("price_source_at_close", pd.Series(pd.NA, index=frame.index)).astype("string").fillna("unknown")
    )
    return frame


def summarize_edge(
    *,
    db_path: Path | None = None,
    features_dir: Path | None = None,
    runtime_events_path: Path | None = None,
) -> Dict[str, Any]:
    trades = _build_trade_context(db_path=db_path, features_dir=features_dir)
    events = load_runtime_events(runtime_events_path)

    overview = {
        "closed_trades": int(len(trades)),
        "win_rate_pct": _round((pd.to_numeric(trades["computed_total_pnl_pct"], errors="coerce") > 0).mean() * 100.0, 3)
        if not trades.empty
        else None,
        "avg_pnl_pct": _round(pd.to_numeric(trades.get("computed_total_pnl_pct"), errors="coerce").mean(), 3)
        if not trades.empty
        else None,
        "median_pnl_pct": _round(pd.to_numeric(trades.get("computed_total_pnl_pct"), errors="coerce").median(), 3)
        if not trades.empty
        else None,
        "avg_giveback_pct": _round(pd.to_numeric(trades.get("giveback_pct"), errors="coerce").mean(), 3)
        if not trades.empty
        else None,
    }

    coverage_rows: list[dict[str, Any]] = []
    if not trades.empty:
        coverage_defs = [
            ("holders", "feat_holders", "feat_missing_holders"),
            ("txns_last_5m", "feat_txns_last_5m", None),
            ("rug_score", "feat_rug_score", "feat_missing_rug_score"),
            ("socials", "feat_social_ok", "feat_missing_socials"),
            ("trend", "feat_trend", "feat_missing_trend"),
        ]
        for label, value_col, missing_col in coverage_defs:
            present = _presence_from_snapshot(trades, value_col, missing_col)
            coverage_rows.append(
                {
                    "field": label,
                    "present_count": int(present.sum()),
                    "present_pct": _round(present.mean() * 100.0, 3),
                }
            )

    partial_series = (
        pd.to_numeric(trades["partial_taken"], errors="coerce").fillna(0)
        if "partial_taken" in trades.columns
        else pd.Series(0, index=trades.index, dtype="float64")
    )
    partial_taken_series = (
        pd.to_numeric(trades["partial_taken"], errors="coerce").fillna(0)
        if "partial_taken" in trades.columns
        else pd.Series(0, index=trades.index, dtype="float64")
    )
    computed_pnl_series = (
        pd.to_numeric(trades["computed_total_pnl_pct"], errors="coerce").fillna(0.0)
        if "computed_total_pnl_pct" in trades.columns
        else pd.Series(0.0, index=trades.index, dtype="float64")
    )
    partial_rows = _group_trade_stats(
        trades.assign(partial_group=partial_series.astype(int).map({0: "no", 1: "yes"})),
        "partial_group",
    )
    partial_negative_after_win = int(
        (
            partial_taken_series.astype(int).eq(1)
            & computed_pnl_series.lt(0.0)
        ).sum()
    ) if not trades.empty else 0
    partial_taken_count = int(partial_taken_series.astype(int).eq(1).sum()) if not trades.empty else 0

    winners = trades[computed_pnl_series.gt(0.0)].copy()
    winner_giveback = pd.to_numeric(winners.get("giveback_pct"), errors="coerce") if not winners.empty else pd.Series(dtype="float64")

    requeue_summary: dict[str, Any] = {
        "events_path": str(runtime_events_path or RUNTIME_EVENTS_PATH),
        "rows": 0,
        "requeue_rows": [],
        "addresses_requeued": 0,
        "addresses_bought_after_requeue": 0,
        "avg_minutes_first_seen_to_buy": None,
        "avg_requeues_before_buy": None,
    }
    if not events.empty:
        requeue_summary["rows"] = int(len(events))
        event_type = events["event_type"].astype("string") if "event_type" in events.columns else pd.Series("", index=events.index)
        requeues = events[event_type == "requeue"].copy()
        buys = events[event_type == "buy"].copy()
        buy_addresses = set(buys["address"].astype("string")) if not buys.empty else set()
        requeue_summary["addresses_requeued"] = int(requeues["address"].nunique()) if not requeues.empty else 0
        requeue_summary["addresses_bought_after_requeue"] = int(requeues[requeues["address"].isin(buy_addresses)]["address"].nunique()) if not requeues.empty else 0

        if not requeues.empty:
            rows: list[dict[str, Any]] = []
            for reason, group in requeues.groupby(requeues["reason"].fillna("unknown").astype("string")):
                unique_addresses = int(group["address"].nunique())
                bought_addresses = int(group[group["address"].isin(buy_addresses)]["address"].nunique())
                rows.append(
                    {
                        "reason": str(reason),
                        "events": int(len(group)),
                        "unique_addresses": unique_addresses,
                        "bought_after_requeue": bought_addresses,
                        "conversion_pct": _round((bought_addresses / unique_addresses * 100.0) if unique_addresses else None, 3),
                        "avg_backoff_s": _round(pd.to_numeric(group.get("backoff_s"), errors="coerce").mean(), 3),
                    }
                )
            rows.sort(key=lambda row: (-int(row["events"]), str(row["reason"])))
            requeue_summary["requeue_rows"] = rows

        if not buys.empty:
            buy_first = buys.sort_values("ts_utc", kind="mergesort").drop_duplicates(subset=["address"], keep="first").copy()
            if "first_seen_utc" in buy_first.columns:
                latency = (buy_first["ts_utc"] - buy_first["first_seen_utc"]).dt.total_seconds() / 60.0
                latency = latency[latency.notna() & latency.ge(0)]
                if not latency.empty:
                    requeue_summary["avg_minutes_first_seen_to_buy"] = _round(latency.mean(), 3)
            if not requeues.empty:
                counts = requeues.groupby("address").size()
                requeued_buys = counts[counts.index.isin(buy_addresses)]
                if not requeued_buys.empty:
                    requeue_summary["avg_requeues_before_buy"] = _round(requeued_buys.mean(), 3)

    return {
        "project_root": str(PROJECT_ROOT),
        "overview": overview,
        "exit_reason": _group_trade_stats(trades, "exit_reason"),
        "price_sources_buy": _group_trade_stats(trades, "price_source_at_buy"),
        "price_sources_close": _group_trade_stats(trades, "price_source_at_close"),
        "price_source_pairs": _group_trade_stats(trades, "price_source_pair"),
        "regimes": {
            "discovered_via": _group_trade_stats(trades, "report_discovered_via"),
            "entry_regime": _group_trade_stats(trades, "report_entry_regime"),
            "entry_lane": _group_trade_stats(trades, "report_entry_lane"),
            "gate_profile": _group_trade_stats(trades, "report_gate_profile"),
            "dex_id": _group_trade_stats(trades, "report_dex_id"),
            "buy_dex_id": _group_trade_stats(trades, "report_buy_dex_id"),
            "liquidity_proxy": _group_trade_stats(trades, "liquidity_proxy_bucket"),
            "age_bucket": _group_trade_stats(trades, "age_bucket"),
            "liquidity_bucket": _group_trade_stats(trades, "liquidity_bucket"),
            "market_cap_bucket": _group_trade_stats(trades, "market_cap_bucket"),
            "price5m_bucket": _group_trade_stats(trades, "price5m_bucket"),
            "txns5m_bucket": _group_trade_stats(trades, "txns5m_bucket"),
            "score_bucket": _group_trade_stats(trades, "score_bucket"),
            "runner_exit_profile": _group_trade_stats(trades, "report_runner_exit_profile"),
            "social_status": _group_trade_stats(trades, "report_social_status"),
        },
        "sizing": {
            "size_bucket": _group_trade_stats(trades, "report_size_bucket"),
            "size_multiplier_bucket": _group_trade_stats(trades, "size_multiplier_bucket"),
            "buy_amount_bucket": _group_trade_stats(trades, "buy_amount_bucket"),
        },
        "coverage": coverage_rows,
        "winners": {
            "count": int(len(winners)),
            "avg_giveback_pct": _round(winner_giveback.mean(), 3) if not winner_giveback.empty else None,
            "median_giveback_pct": _round(winner_giveback.median(), 3) if not winner_giveback.empty else None,
            "giveback_ge_20pct_count": int(winner_giveback.ge(20.0).sum()) if not winner_giveback.empty else 0,
            "giveback_ge_40pct_count": int(winner_giveback.ge(40.0).sum()) if not winner_giveback.empty else 0,
        },
        "partials": {
            "rows": partial_rows,
            "partial_taken_count": partial_taken_count,
            "partial_winner_then_red_count": partial_negative_after_win,
            "partial_winner_then_red_pct": _round((partial_negative_after_win / partial_taken_count * 100.0) if partial_taken_count else None, 3),
        },
        "requeues": requeue_summary,
    }


def render_edge_markdown(snapshot: Dict[str, Any]) -> str:
    overview = snapshot["overview"]
    lines = [
        "# Edge Report",
        "",
        f"- Project root: `{snapshot['project_root']}`",
        f"- Closed trades analysed: `{overview['closed_trades']}`",
        f"- Win rate: `{overview['win_rate_pct']}`",
        f"- Avg PnL (%): `{overview['avg_pnl_pct']}`",
        f"- Median PnL (%): `{overview['median_pnl_pct']}`",
        f"- Avg giveback (%): `{overview['avg_giveback_pct']}`",
        "",
        "## Exit Reason",
        "",
    ]

    def _append_group_rows(rows: list[dict[str, Any]]) -> None:
        if not rows:
            lines.append("- Sin datos")
            lines.append("")
            return
        for row in rows:
            lines.append(
                "- `{group}`: count=`{count}`, win_rate=`{win_rate_pct}`, avg_pnl=`{avg_pnl_pct}`, median_pnl=`{median_pnl_pct}`, sum_pnl_pts=`{sum_pnl_pct_points}`, avg_giveback=`{avg_giveback_pct}`, avg_hold_min=`{avg_hold_minutes}`".format(
                    **row
                )
            )
        lines.append("")

    _append_group_rows(snapshot["exit_reason"])

    lines.extend(["## Regimes", "", "### discovered_via", ""])
    _append_group_rows(snapshot["regimes"]["discovered_via"])
    lines.extend(["### entry_regime", ""])
    _append_group_rows(snapshot["regimes"]["entry_regime"])
    lines.extend(["### entry_lane", ""])
    _append_group_rows(snapshot["regimes"].get("entry_lane", []))
    lines.extend(["### gate_profile", ""])
    _append_group_rows(snapshot["regimes"].get("gate_profile", []))
    lines.extend(["### dex_id", ""])
    _append_group_rows(snapshot["regimes"]["dex_id"])
    lines.extend(["### liquidity_proxy", ""])
    _append_group_rows(snapshot["regimes"].get("liquidity_proxy", []))
    lines.extend(["### age_bucket", ""])
    _append_group_rows(snapshot["regimes"]["age_bucket"])
    lines.extend(["### liquidity_bucket", ""])
    _append_group_rows(snapshot["regimes"]["liquidity_bucket"])
    lines.extend(["### market_cap_bucket", ""])
    _append_group_rows(snapshot["regimes"]["market_cap_bucket"])
    lines.extend(["### price5m_bucket", ""])
    _append_group_rows(snapshot["regimes"].get("price5m_bucket", []))
    lines.extend(["### txns5m_bucket", ""])
    _append_group_rows(snapshot["regimes"].get("txns5m_bucket", []))
    lines.extend(["### score_bucket", ""])
    _append_group_rows(snapshot["regimes"]["score_bucket"])
    lines.extend(["### runner_exit_profile", ""])
    _append_group_rows(snapshot["regimes"].get("runner_exit_profile", []))

    lines.extend(["## Sizing", "", "### size_bucket", ""])
    _append_group_rows(snapshot["sizing"]["size_bucket"])
    lines.extend(["### size_multiplier_bucket", ""])
    _append_group_rows(snapshot["sizing"]["size_multiplier_bucket"])
    lines.extend(["### buy_amount_bucket", ""])
    _append_group_rows(snapshot["sizing"]["buy_amount_bucket"])

    lines.extend(["## Price Sources", "", "### Buy", ""])
    _append_group_rows(snapshot["price_sources_buy"])
    lines.extend(["### Close", ""])
    _append_group_rows(snapshot["price_sources_close"])
    lines.extend(["### Buy -> Close", ""])
    _append_group_rows(snapshot["price_source_pairs"])

    lines.extend(["## Data Coverage", ""])
    if snapshot["coverage"]:
        for row in snapshot["coverage"]:
            lines.append(
                f"- `{row['field']}`: present=`{row['present_count']}`, pct=`{row['present_pct']}`"
            )
    else:
        lines.append("- Sin datos")
    lines.append("")

    winners = snapshot["winners"]
    lines.extend(
        [
            "## Winners Giveback",
            "",
            f"- Winners count: `{winners['count']}`",
            f"- Avg giveback winners (%): `{winners['avg_giveback_pct']}`",
            f"- Median giveback winners (%): `{winners['median_giveback_pct']}`",
            f"- Winners with giveback >=20%%: `{winners['giveback_ge_20pct_count']}`",
            f"- Winners with giveback >=40%%: `{winners['giveback_ge_40pct_count']}`",
            "",
        ]
    )

    partials = snapshot["partials"]
    lines.extend(
        [
            "## Partials",
            "",
            f"- Partial taken count: `{partials['partial_taken_count']}`",
            f"- Partial winner then red count: `{partials['partial_winner_then_red_count']}`",
            f"- Partial winner then red pct: `{partials['partial_winner_then_red_pct']}`",
            "",
        ]
    )
    _append_group_rows(partials["rows"])

    requeues = snapshot["requeues"]
    lines.extend(
        [
            "## Requeues",
            "",
            f"- Events path: `{requeues['events_path']}`",
            f"- Runtime events rows: `{requeues['rows']}`",
            f"- Unique addresses requeued: `{requeues['addresses_requeued']}`",
            f"- Unique addresses bought after requeue: `{requeues['addresses_bought_after_requeue']}`",
            f"- Avg minutes first_seen -> buy: `{requeues['avg_minutes_first_seen_to_buy']}`",
            f"- Avg requeues before buy: `{requeues['avg_requeues_before_buy']}`",
            "",
            "### By Reason",
            "",
        ]
    )
    if requeues["requeue_rows"]:
        for row in requeues["requeue_rows"]:
            lines.append(
                f"- `{row['reason']}`: events=`{row['events']}`, addresses=`{row['unique_addresses']}`, bought_after_requeue=`{row['bought_after_requeue']}`, conversion_pct=`{row['conversion_pct']}`, avg_backoff_s=`{row['avg_backoff_s']}`"
            )
    else:
        lines.append("- Sin datos")
    lines.append("")
    return "\n".join(lines)
