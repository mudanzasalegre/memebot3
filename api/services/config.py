from __future__ import annotations

import datetime as dt

from analytics.exit_policy import describe_exit_policy
from analytics.filters import describe_filter_policy
from analytics.reporting import snapshot_effective_config
import analytics.research_runtime as research_runtime
from analytics.sizing import describe_sizing_policy
from analytics.strategy_runtime import describe_strategy_policy
from config.config import CFG

from api.repositories.filesystem import file_mtime
from api.schemas.common import Envelope
from api.services.common import build_envelope, make_source_status
from api.settings import APISettings


def _config_env_status(settings: APISettings):
    env_path = (settings.project_root / ".env").resolve()
    if env_path.exists():
        return make_source_status(
            source_key="config.env",
            kind="config",
            status="ok",
            updated_at=file_mtime(env_path),
            detail="project_dotenv",
            path=env_path,
        )
    return make_source_status(
        source_key="config.env",
        kind="config",
        status="empty",
        detail="project_dotenv_missing",
        path=env_path,
    )


def _config_runtime_status(settings: APISettings):
    config_path = (settings.project_root / "config" / "config.py").resolve()
    return make_source_status(
        source_key="config.cfg",
        kind="config",
        status="ok",
        updated_at=file_mtime(config_path),
        detail="loaded_from_env_and_defaults",
        path=config_path,
    )


def get_effective_config_envelope(settings: APISettings) -> Envelope:
    statuses = [
        _config_env_status(settings),
        _config_runtime_status(settings),
    ]
    return build_envelope(snapshot_effective_config(), source_status=statuses)


def _execution_profile() -> dict[str, object]:
    strategy = describe_strategy_policy()
    return {
        "profile": "aggressive_sniper",
        "primary_productive_regimes": ["pump_early"],
        "shadow_only_regimes": [
            regime
            for regime in ("dex_mature", "revival")
            if str((strategy.get(regime) or {}).get("mode") or "shadow") != "off"
        ],
        "requested_modes": {regime: (strategy.get(regime) or {}).get("mode") for regime in strategy},
        "auto_demote_action": str(getattr(CFG, "REGIME_HEALTH_DISABLE_ACTION", "shadow") or "shadow"),
        "live_entry_mode": str(getattr(CFG, "PUMP_EARLY_SNIPER_MODE", "canary_aggressive") or "canary_aggressive"),
        "trade_amount_mode": "fixed",
        "default_trade_amount_sol": float(getattr(CFG, "TRADE_AMOUNT_SOL", 0.1) or 0.1),
        "min_buy_sol": float(getattr(CFG, "MIN_BUY_SOL", 0.1) or 0.1),
        "multipliers_affect_trade_amount": False,
        "max_live_positions": {
            "pump_early": int(getattr(CFG, "PUMP_EARLY_MAX_ACTIVE_POSITIONS", 1) or 1),
            "dex_mature": int(getattr(CFG, "DEX_MATURE_MAX_ACTIVE_POSITIONS", 0) or 0),
            "revival": int(getattr(CFG, "REVIVAL_MAX_ACTIVE_POSITIONS", 0) or 0),
        },
    }


def _sniper_lane() -> dict[str, object]:
    return {
        "enabled": bool(getattr(CFG, "PUMP_EARLY_SNIPER_ENABLED", True)),
        "mode": str(getattr(CFG, "PUMP_EARLY_SNIPER_MODE", "canary_aggressive") or "canary_aggressive"),
        "entry_lane": "pump_early_sniper",
        "profiles": {
            "sniper_core": {
                "min_age_minutes": float(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_AGE_MIN", 3.0) or 3.0),
                "max_age_minutes": float(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_AGE_MIN", 30.0) or 30.0),
                "min_liquidity_usd": float(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD", 1_500.0) or 1_500.0),
                "min_market_cap_usd": float(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_MARKET_CAP_USD", 2_000.0) or 2_000.0),
                "max_market_cap_usd": float(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_MARKET_CAP_USD", 200_000.0) or 200_000.0),
                "min_score_total": int(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_SCORE_TOTAL", 30) or 30),
                "min_rank_score": float(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_RANK_SCORE", 40.0) or 40.0),
                "min_txns_last_5m": int(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_TXNS_5M", 15) or 15),
                "price_pct_5m_range": [
                    float(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_PRICE_PCT_5M", -20.0) or -20.0),
                    float(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_PRICE_PCT_5M", 240.0) or 240.0),
                ],
                "max_price_impact_pct": float(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT", 20.0) or 20.0),
                "max_snapshot_missing_fields": int(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS", 5) or 5),
                "size_multiplier": float(getattr(CFG, "PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER", 0.20) or 0.20),
            },
            "sniper_micro_momentum": {
                "min_liquidity_usd": float(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MIN_LIQUIDITY_USD", 1_000.0) or 1_000.0),
                "min_volume_24h_usd": float(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MIN_VOLUME_USD_24H", 15_000.0) or 15_000.0),
                "max_market_cap_usd": float(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MAX_MARKET_CAP_USD", 125_000.0) or 125_000.0),
                "min_score_total": int(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MIN_SCORE_TOTAL", 25) or 25),
                "min_rank_score": float(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE", 42.0) or 42.0),
                "min_txns_last_5m": int(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MIN_TXNS_5M", 50) or 50),
                "min_price_pct_5m": float(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MIN_PRICE_PCT_5M", 5.0) or 5.0),
                "max_price_impact_pct": float(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MAX_PRICE_IMPACT_PCT", 15.0) or 15.0),
                "size_multiplier": float(getattr(CFG, "PUMP_EARLY_SNIPER_SIZE_MICRO_MULTIPLIER", 0.10) or 0.10),
            },
            "sniper_hot": {
                "min_rank_score": float(getattr(CFG, "PUMP_EARLY_SNIPER_HOT_MIN_RANK_SCORE", 50.0) or 50.0),
                "min_txns_last_5m": int(getattr(CFG, "PUMP_EARLY_SNIPER_HOT_MIN_TXNS_5M", 100) or 100),
                "price_pct_5m_range": [
                    float(getattr(CFG, "PUMP_EARLY_SNIPER_HOT_MIN_PRICE_PCT_5M", 10.0) or 10.0),
                    float(getattr(CFG, "PUMP_EARLY_SNIPER_HOT_MAX_PRICE_PCT_5M", 120.0) or 120.0),
                ],
                "max_snapshot_missing_fields": int(getattr(CFG, "PUMP_EARLY_SNIPER_HOT_MAX_SNAPSHOT_MISSING_FIELDS", 2) or 2),
                "size_multiplier": float(getattr(CFG, "PUMP_EARLY_SNIPER_SIZE_HOT_MULTIPLIER", 0.30) or 0.30),
            },
        },
        "confirmation": {
            "fast_confirm_snapshots": 1,
            "fast_confirm_min_age_minutes": float(getattr(CFG, "PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_AGE_MIN", 3.0) or 3.0),
            "fast_confirm_min_txns_last_5m": int(getattr(CFG, "PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_TXNS_5M", 40) or 40),
            "fallback_snapshots": int(getattr(CFG, "PUMP_EARLY_CONFIRM_SNAPSHOTS", 2) or 2),
        },
        "capacity": {
            "paper": int(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_OPEN_PAPER", 3) or 3),
            "live_canary": int(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_OPEN_LIVE_CANARY", 1) or 1),
            "live_canary_advanced": int(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_OPEN_LIVE_CANARY_ADVANCED", 2) or 2),
        },
        "paper_learning": {
            "continue_on_health": bool(getattr(CFG, "PUMP_EARLY_SNIPER_PAPER_CONTINUE_ON_HEALTH", True)),
            "recovery_size_cap": float(getattr(CFG, "PUMP_EARLY_SNIPER_PAPER_RECOVERY_SIZE_CAP", 0.20) or 0.20),
            "route_proxy_liquidity_enabled": bool(
                getattr(CFG, "PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_ENABLED", True)
            ),
            "route_proxy_liquidity_usd": float(
                getattr(CFG, "PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_USD", 1_500.0) or 1_500.0
            ),
        },
    }


def _profit_lane() -> dict[str, object]:
    exit_policy = describe_exit_policy()
    runner_profiles = dict(exit_policy.get("profit_lane_runner_profiles") or {})
    return {
        "enabled": bool(getattr(CFG, "PUMP_EARLY_PROFIT_LANE_ENABLED", True)),
        "entry_lane": "pump_early_pumpswap_profit",
        "prime_label": "pump_early_pumpswap_prime",
        "runner_exit_profiles": {
            "pump_early_pumpswap_profit": {
                "default_profile": "broad_runner",
                "profiles": runner_profiles,
            },
            "pump_early_pumpswap_prime": {
                "default_profile": "prime_runner",
                "profiles": runner_profiles,
            },
            "pumpswap_meteor_prime": {
                "default_profile": "meteor_runner",
                "profiles": runner_profiles,
            },
        },
        "profiles": {
            "pumpswap_profit_broad": {
                "dex_allowlist": str(getattr(CFG, "PUMP_EARLY_PROFIT_DEX_ALLOWLIST", "pumpswap") or "pumpswap"),
                "require_jupiter_route": True,
                "require_real_liquidity": bool(getattr(CFG, "PUMP_EARLY_PROFIT_REQUIRE_REAL_LIQUIDITY", True)),
                "min_liquidity_usd": float(getattr(CFG, "PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD", 5_000.0) or 5_000.0),
                "min_score_total": int(getattr(CFG, "PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL", 35) or 35),
                "age_minutes_range": [
                    float(getattr(CFG, "PUMP_EARLY_PROFIT_MIN_AGE_MIN", 3.0) or 3.0),
                    float(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_AGE_MIN", 30.0) or 30.0),
                ],
                "max_price_impact_pct": float(
                    getattr(CFG, "PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT", 10.0) or 10.0
                ),
                "blocked_market_cap_usd_range": [
                    float(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD", 25_000.0) or 25_000.0),
                    float(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD", 50_000.0) or 50_000.0),
                ],
                "blocked_price_pct_5m_ranges": str(
                    getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES", "0:25,50:100") or "0:25,50:100"
                ),
                "size_multiplier": float(getattr(CFG, "PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER", 0.20) or 0.20),
                "effective_trade_amount_sol": float(getattr(CFG, "TRADE_AMOUNT_SOL", 0.1) or 0.1),
            },
            "pumpswap_profit_prime": {
                "market_cap_usd_max": 25_000.0,
                "liquidity_usd_range": [
                    float(getattr(CFG, "PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD", 5_000.0) or 5_000.0),
                    25_000.0,
                ],
                "size_multiplier": float(getattr(CFG, "PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER", 0.20) or 0.20),
                "effective_trade_amount_sol": float(getattr(CFG, "TRADE_AMOUNT_SOL", 0.1) or 0.1),
                "no_size_promotion_yet": True,
            },
            "pumpswap_meteor_prime": {
                "enabled": bool(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_ENABLED", True)),
                "purpose": "METEOR-like low-mcap high-velocity pumpswap breakouts",
                "dex_allowlist": "pumpswap",
                "require_jupiter_route": True,
                "require_real_liquidity": True,
                "liquidity_usd_range": [
                    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_LIQUIDITY_USD", 4_000.0) or 4_000.0),
                    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_LIQUIDITY_USD", 30_000.0) or 30_000.0),
                ],
                "market_cap_usd_range": [
                    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_MARKET_CAP_USD", 5_000.0) or 5_000.0),
                    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_MARKET_CAP_USD", 30_000.0) or 30_000.0),
                ],
                "price_pct_5m_range": [
                    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_PRICE_PCT_5M", 110.0) or 110.0),
                    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_PCT_5M", 300.0) or 300.0),
                ],
                "min_txns_last_5m": int(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_TXNS_5M", 220) or 220),
                "min_score_total": int(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_SCORE_TOTAL", 30) or 30),
                "age_minutes_range": [
                    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_AGE_MIN", 3.0) or 3.0),
                    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_AGE_MIN", 18.0) or 18.0),
                ],
                "max_price_impact_pct": float(
                    getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_IMPACT_PCT", 12.0) or 12.0
                ),
                "min_volume_24h_usd": float(
                    getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_VOLUME_USD_24H", 8_000.0) or 8_000.0
                ),
                "size_bucket": "pumpswap_meteor",
                "effective_trade_amount_sol": float(getattr(CFG, "TRADE_AMOUNT_SOL", 0.1) or 0.1),
            },
        },
        "shape_guard": {
            "enabled": bool(getattr(CFG, "PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED", True)),
            "health_rebase_current_gate": bool(
                getattr(CFG, "PUMP_EARLY_PROFIT_HEALTH_REBASE_CURRENT_GATE", True)
            ),
            "max_market_cap_usd": float(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD", 500_000.0) or 500_000.0),
            "deep_negative_price5m": {
                "price_pct_5m_lte": float(getattr(CFG, "PUMP_EARLY_PROFIT_DEEP_NEG_PRICE5M_PCT", -40.0)),
                "unless_txns_last_5m_gte": int(
                    getattr(CFG, "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_TXNS_5M", 1_500) or 1_500
                ),
                "unless_volume_24h_usd_gte": float(
                    getattr(CFG, "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_VOLUME_USD_24H", 150_000.0) or 150_000.0
                ),
            },
            "extreme_spike_high_mcap": {
                "price_pct_5m_gte": float(getattr(CFG, "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_PCT", 300.0) or 300.0),
                "market_cap_usd_gte": float(
                    getattr(CFG, "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_MIN_MCAP_USD", 100_000.0) or 100_000.0
                ),
            },
            "dead_volume": {
                "volume_24h_usd_range": [
                    float(getattr(CFG, "PUMP_EARLY_PROFIT_DEAD_VOLUME_MIN_USD_24H", 15_000.0) or 15_000.0),
                    float(getattr(CFG, "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_USD_24H", 30_000.0) or 30_000.0),
                ],
                "max_txns_last_5m": int(
                    getattr(CFG, "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_TXNS_5M", 1_000) or 1_000
                ),
            },
            "hot_requires_support": {
                "price_pct_5m_range": [
                    float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_PRICE5M_MIN_PCT", 100.0) or 100.0),
                    float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_PRICE5M_MAX_PCT", 180.0) or 180.0),
                ],
                "market_cap_usd_gte": float(
                    getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MCAP_MIN_USD", 50_000.0) or 50_000.0
                ),
                "min_liquidity_usd": float(
                    getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MIN_LIQUIDITY_USD", 20_000.0) or 20_000.0
                ),
                "min_txns_last_5m": int(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MIN_TXNS_5M", 600) or 600),
                "min_volume_24h_usd": float(
                    getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MIN_VOLUME_USD_24H", 50_000.0) or 50_000.0
                ),
            },
            "low_volume_no_momentum": {
                "volume_24h_usd_lt": float(
                    getattr(CFG, "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H", 15_000.0)
                    or 15_000.0
                ),
                "txns_last_5m_lt": int(
                    getattr(CFG, "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_TXNS_5M", 500) or 500
                ),
                "price_pct_5m_lt": float(
                    getattr(CFG, "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_PRICE5M_PCT", 50.0) or 50.0
                ),
            },
            "prime_mid_momentum_requires_support": {
                "market_cap_usd_lt": 25_000.0,
                "price_pct_5m_range": [25.0, 50.0],
                "min_txns_last_5m": int(
                    getattr(CFG, "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_TXNS_5M", 350) or 350
                ),
                "min_volume_24h_usd": float(
                    getattr(CFG, "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_VOLUME_USD_24H", 100_000.0)
                    or 100_000.0
                ),
            },
            "high_mcap_mid_momentum_block": {
                "market_cap_usd_gte": float(
                    getattr(CFG, "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD", 100_000.0) or 100_000.0
                ),
                "price_pct_5m_range": [
                    float(getattr(CFG, "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT", 40.0) or 40.0),
                    float(getattr(CFG, "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT", 50.0) or 50.0),
                ],
            },
            "meteor_prime_bypasses_shape_guard": True,
        },
        "capacity": {
            "paper": int(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_OPEN_PAPER", 2) or 2),
            "live_canary": int(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_OPEN_LIVE_CANARY", 1) or 1),
        },
        "exits": {
            "adverse_tick_after_s": int(getattr(CFG, "PUMP_EARLY_PROFIT_ADVERSE_TICK_AFTER_S", 75) or 75),
            "adverse_tick_pnl_pct": float(getattr(CFG, "PUMP_EARLY_PROFIT_ADVERSE_TICK_PNL_PCT", -8.0) or -8.0),
            "no_pump_window_min": float(getattr(CFG, "PUMP_EARLY_PROFIT_NO_PUMP_WINDOW_MIN", 3.0) or 3.0),
            "no_pump_min_peak_pct": float(getattr(CFG, "PUMP_EARLY_PROFIT_NO_PUMP_MIN_PEAK_PCT", 2.0) or 2.0),
            "no_pump_max_pnl_pct": float(getattr(CFG, "PUMP_EARLY_PROFIT_NO_PUMP_MAX_PNL_PCT", 0.0) or 0.0),
            "partial_trigger_pct": float(getattr(CFG, "PUMP_EARLY_TP_PARTIAL_TRIGGER_PCT", 4.0) or 4.0),
            "partial_fraction": float(getattr(CFG, "PUMP_EARLY_TP_PARTIAL_FRACTION", 0.80) or 0.80),
            "runner_profiles": runner_profiles,
        },
    }


def _live_profit_gate() -> dict[str, object]:
    live_min_age = max(8.0, float(getattr(CFG, "PUMP_EARLY_LIVE_HARD_MIN_AGE_MIN", 8.0) or 8.0))
    live_min_score = max(50, int(getattr(CFG, "PUMP_EARLY_LIVE_HARD_MIN_SCORE_TOTAL", 50) or 50))
    live_min_liq = max(
        10_000.0,
        float(getattr(CFG, "PUMP_EARLY_LIVE_HARD_MIN_LIQUIDITY_USD", 10_000.0) or 10_000.0),
    )
    live_min_mcap = max(
        20_000.0,
        float(getattr(CFG, "PUMP_EARLY_QUALITY_MIN_MARKET_CAP_USD", 20_000.0) or 20_000.0),
    )
    return {
        "enabled_for_live": True,
        "enabled_for_shadow": False,
        "regimes": {
            "pump_early": {
                "replaced_by": "sniper_lane",
                "has_jupiter_route": True,
                "min_age_minutes": live_min_age,
                "min_score_total": live_min_score,
                "min_liquidity_usd": live_min_liq,
                "min_market_cap_usd": live_min_mcap,
                "max_market_cap_usd": float(
                    getattr(CFG, "PUMP_EARLY_LIVE_HARD_MAX_MARKET_CAP_USD", 125_000.0) or 125_000.0
                ),
                "max_price_impact_pct": float(
                    getattr(CFG, "PUMP_EARLY_LIVE_HARD_MAX_PRICE_IMPACT_PCT", 10.0) or 10.0
                ),
                "max_snapshot_missing_fields": int(
                    getattr(CFG, "PUMP_EARLY_LIVE_MAX_SNAPSHOT_MISSING_FIELDS", 3) or 3
                ),
            }
        },
        "paper_cold_start": {
            "enabled": bool(getattr(CFG, "PAPER_COLD_START_ENABLED", True)),
            "dry_run_only": True,
            "configured_dry_run": bool(getattr(CFG, "DRY_RUN", False)),
            "active_until_closed_trades": int(getattr(CFG, "PAPER_COLD_START_MAX_CLOSED_TRADES", 50) or 50),
            "regimes": {
                "pump_early": {
                    "has_jupiter_route": True,
                    "min_age_minutes": float(getattr(CFG, "PAPER_COLD_START_MIN_AGE_MIN", 12.0) or 12.0),
                    "min_score_total": int(getattr(CFG, "PAPER_COLD_START_MIN_SCORE_TOTAL", 45) or 45),
                    "min_liquidity_usd": float(
                        getattr(CFG, "PAPER_COLD_START_MIN_LIQUIDITY_USD", 10_000.0) or 10_000.0
                    ),
                    "min_market_cap_usd": float(
                        getattr(CFG, "PAPER_COLD_START_MIN_MARKET_CAP_USD", 15_000.0) or 15_000.0
                    ),
                    "max_market_cap_usd": float(
                        getattr(CFG, "PUMP_EARLY_LIVE_HARD_MAX_MARKET_CAP_USD", 125_000.0) or 125_000.0
                    ),
                    "max_price_impact_pct": float(
                        getattr(CFG, "PUMP_EARLY_LIVE_HARD_MAX_PRICE_IMPACT_PCT", 10.0) or 10.0
                    ),
                    "max_snapshot_missing_fields": int(
                        getattr(CFG, "PAPER_COLD_START_MAX_SNAPSHOT_MISSING_FIELDS", 4) or 4
                    ),
                    "min_rank_score": float(getattr(CFG, "PAPER_COLD_START_MIN_RANK_SCORE", 12.5) or 12.5),
                    "require_price_pct_5m": bool(getattr(CFG, "PAPER_COLD_START_REQUIRE_PRICE_PCT_5M", True)),
                    "min_price_pct_5m": float(getattr(CFG, "PAPER_COLD_START_MIN_PRICE_PCT_5M", 0.0) or 0.0),
                    "max_price_pct_5m": float(getattr(CFG, "PAPER_COLD_START_MAX_PRICE_PCT_5M", 80.0) or 80.0),
                    "shadow_probe_enabled": bool(getattr(CFG, "PAPER_COLD_START_SHADOW_PROBE_ENABLED", True)),
                    "shadow_probe_size_multiplier": float(
                        getattr(CFG, "PAPER_COLD_START_SHADOW_PROBE_SIZE_MULTIPLIER", 0.10) or 0.10
                    ),
                }
            },
        },
    }


def _rank_gate() -> dict[str, object]:
    pump_rank_gate = research_runtime.load_live_rank_gate("pump_early", now=dt.datetime.now(dt.timezone.utc))
    return {
        "enabled_for_live": True,
        "fallback_threshold": float(getattr(CFG, "LIVE_RANK_SCORE_FALLBACK_MIN", 12.5) or 12.5),
        "min_selected_rows": int(getattr(CFG, "LIVE_RANK_SCORE_MIN_SELECTED_ROWS", 20) or 20),
        "min_avg_pnl_pct": float(getattr(CFG, "LIVE_RANK_SCORE_MIN_AVG_PNL_PCT", 3.0) or 3.0),
        "pump_early": pump_rank_gate,
    }


def _research_lane() -> dict[str, object]:
    return {
        "enabled": bool(getattr(CFG, "RESEARCH_LANE_ENABLED", True)),
        "shadow_enabled": bool(getattr(CFG, "RESEARCH_SHADOW_ENABLED", True)),
        "eligible_regimes": ["pump_early"],
        "routes": ["pump_early_sniper_research", "dex_mature_shadow", "revival_shadow"],
        "max_open": int(getattr(CFG, "RESEARCH_SHADOW_MAX_OPEN", 6) or 6),
        "max_open_per_regime": int(getattr(CFG, "RESEARCH_SHADOW_MAX_OPEN_PER_REGIME", 4) or 4),
        "min_rank_score": float(getattr(CFG, "RESEARCH_SHADOW_MIN_RANK_SCORE", 55.0) or 55.0),
        "min_age_minutes": float(getattr(CFG, "RESEARCH_SHADOW_MIN_AGE_MIN", 2.0) or 2.0),
        "min_liquidity_usd": float(getattr(CFG, "RESEARCH_SHADOW_MIN_LIQUIDITY_USD", 1500.0) or 1500.0),
        "near_miss_score_margin": int(getattr(CFG, "RESEARCH_NEAR_MISS_SCORE_MARGIN", 8) or 8),
        "near_miss_proba_margin": float(getattr(CFG, "RESEARCH_NEAR_MISS_PROBA_MARGIN", 0.12) or 0.12),
        "allow_proxy_liquidity_research": bool(getattr(CFG, "PUMP_EARLY_RESEARCH_ALLOW_PROXY", True)),
    }


def _paper_validation() -> dict[str, object]:
    return {
        "strict_health": bool(getattr(CFG, "PAPER_PNL_STRICT_HEALTH", True)),
        "productive_portfolio_lane": "pump_early_pumpswap_profit",
        "research_shadow_separate": True,
        "paper_continue_on_health": bool(getattr(CFG, "PUMP_EARLY_SNIPER_PAPER_CONTINUE_ON_HEALTH", True)),
        "effective_continue_on_health": bool(
            getattr(CFG, "PUMP_EARLY_SNIPER_PAPER_CONTINUE_ON_HEALTH", True)
            and not bool(getattr(CFG, "PAPER_PNL_STRICT_HEALTH", True))
        ),
    }


def get_policies_envelope(settings: APISettings) -> Envelope:
    config_path = (settings.project_root / "config" / "config.py").resolve()
    data = {
        "filters": describe_filter_policy(),
        "sizing": describe_sizing_policy(),
        "exit": describe_exit_policy(),
        "strategy": describe_strategy_policy(),
        "execution_profile": _execution_profile(),
        "sniper_lane": _sniper_lane(),
        "profit_lane": _profit_lane(),
        "live_profit_gate": _live_profit_gate(),
        "rank_gate": _rank_gate(),
        "research_lane": _research_lane(),
        "paper_validation": _paper_validation(),
    }
    statuses = [
        _config_env_status(settings),
        _config_runtime_status(settings),
        make_source_status(
            source_key="config.policies",
            kind="config",
            status="ok",
            updated_at=file_mtime(config_path),
            detail="derived_from_cfg",
            path=config_path,
        )
    ]
    return build_envelope(data, source_status=statuses)
