from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

from analytics.profit_pnl_guard import evaluate_profit_pnl_guard
from analytics.pumpswap_prime_strict import evaluate_pumpswap_prime_strict
from analytics.pumpswap_rebound_prime import (
    apply_pumpswap_rebound_prime_context,
    apply_pumpswap_rebound_watch_context,
    evaluate_pumpswap_rebound_prime,
)


def _load_entry_quality_gate(**overrides: object):
    namespace = _load_quality_namespace(**overrides)
    return namespace["_entry_quality_gate"]


def _load_quality_namespace(**overrides: object):
    source = Path("run_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="run_bot.py")
    wanted = {
        "_metric_float",
        "_metric_optional_float",
        "_metric_int",
        "_candidate_age_minutes",
        "_paper_cold_start_active",
        "_paper_cold_start_shadow_probe_allowed",
        "_add_min_failure",
        "_add_max_failure",
        "_sniper_rank_score",
        "_evaluate_sniper_core",
        "_evaluate_sniper_micro",
        "_sniper_hot_ok",
        "_parse_float_ranges",
        "_gate_dex_id",
        "_is_liquidity_proxy",
        "_mcap_bucket",
        "_price5m_bucket",
        "_price5m_blocked_bucket",
        "_aggressive_research_guard_failures",
        "_meteor_prime_failures",
        "_breakout_probe_failures",
        "_profit_shape_guard_failures",
        "_set_profit_gate_context",
        "_evaluate_pumpswap_profit_gate",
        "_tag_pump_sniper_gate",
        "_aggressive_pump_gate",
        "_paper_aggressive_pump_gate",
        "_live_aggressive_pump_gate",
        "_entry_quality_gate",
    }
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    module = ast.Module(body=body, type_ignores=[])
    namespace = {
        "dt": dt,
        "parse_iso_utc": lambda raw: dt.datetime.fromisoformat(raw) if raw else None,
        "utc_now": lambda: dt.datetime(2026, 4, 6, 15, 0, tzinfo=dt.timezone.utc),
        "research_runtime": SimpleNamespace(
            load_live_rank_gate=lambda regime: {
                "threshold": 12.5,
                "source": "fallback",
                "enabled": True,
            }
        ),
        "evaluate_profit_pnl_guard": evaluate_profit_pnl_guard,
        "evaluate_pumpswap_prime_strict": evaluate_pumpswap_prime_strict,
        "evaluate_pumpswap_rebound_prime": evaluate_pumpswap_rebound_prime,
        "apply_pumpswap_rebound_prime_context": apply_pumpswap_rebound_prime_context,
        "apply_pumpswap_rebound_watch_context": apply_pumpswap_rebound_watch_context,
        "DRY_RUN": False,
        "_stats": {"sold": 0},
        "_PUMP_EARLY_LIVE_HARD_MIN_AGE_MIN": 8.0,
        "_PUMP_EARLY_LIVE_HARD_MIN_LIQUIDITY_USD": 10_000.0,
        "_PUMP_EARLY_LIVE_HARD_MIN_SCORE_TOTAL": 50,
        "_PUMP_EARLY_LIVE_HARD_MIN_VOLUME_USD_24H": 0.0,
        "_PUMP_EARLY_LIVE_MIN_AGE_EFFECTIVE": 8.0,
        "_PUMP_EARLY_LIVE_MIN_LIQUIDITY_EFFECTIVE": 10_000.0,
        "_PUMP_EARLY_LIVE_MIN_SCORE_EFFECTIVE": 50.0,
        "_PUMP_EARLY_LIVE_MIN_MARKET_CAP_EFFECTIVE": 20_000.0,
        "_PUMP_EARLY_LIVE_HARD_MAX_MARKET_CAP_USD": 125_000.0,
        "_PUMP_EARLY_LIVE_HARD_MAX_PRICE_IMPACT_PCT": 10.0,
        "_PUMP_EARLY_LIVE_MAX_SNAPSHOT_MISSING_FIELDS": 3,
        "_PAPER_COLD_START_ENABLED": True,
        "_PAPER_COLD_START_MAX_CLOSED_TRADES": 50,
        "_PAPER_COLD_START_MIN_AGE_MIN": 12.0,
        "_PAPER_COLD_START_MIN_SCORE_TOTAL": 45.0,
        "_PAPER_COLD_START_MIN_LIQUIDITY_USD": 10_000.0,
        "_PAPER_COLD_START_MIN_MARKET_CAP_USD": 15_000.0,
        "_PAPER_COLD_START_MAX_SNAPSHOT_MISSING_FIELDS": 4,
        "_PAPER_COLD_START_MIN_RANK_SCORE": 12.5,
        "_PAPER_COLD_START_REQUIRE_PRICE_PCT_5M": True,
        "_PAPER_COLD_START_MIN_PRICE_PCT_5M": 0.0,
        "_PAPER_COLD_START_MAX_PRICE_PCT_5M": 80.0,
        "_PAPER_COLD_START_SHADOW_PROBE_ENABLED": True,
        "_PAPER_COLD_START_SHADOW_PROBE_SIZE_MULTIPLIER": 0.10,
        "_PAPER_AGGRESSIVE_TRADING_ENABLED": False,
        "_PAPER_AGGRESSIVE_MIN_AGE_MIN": 0.05,
        "_PAPER_AGGRESSIVE_MIN_LIQUIDITY_USD": 1_500.0,
        "_PAPER_AGGRESSIVE_MIN_MARKET_CAP_USD": 2_000.0,
        "_PAPER_AGGRESSIVE_MAX_MARKET_CAP_USD": 500_000.0,
        "_PAPER_AGGRESSIVE_MIN_SCORE_TOTAL": 30,
        "_PAPER_AGGRESSIVE_MIN_RANK_SCORE": 35.0,
        "_PAPER_AGGRESSIVE_MIN_TXNS_5M": 3,
        "_PAPER_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS": 5,
        "_PAPER_AGGRESSIVE_MAX_PRICE_IMPACT_PCT": 20.0,
        "_PAPER_AGGRESSIVE_REQUIRE_ROUTE": True,
        "_PAPER_AGGRESSIVE_REQUIRE_PRICE": True,
        "_PAPER_AGGRESSIVE_BUY_RESEARCH_LANES": True,
        "_LIVE_AGGRESSIVE_TRADING_ENABLED": False,
        "_LIVE_AGGRESSIVE_MIN_AGE_MIN": 0.05,
        "_LIVE_AGGRESSIVE_MIN_LIQUIDITY_USD": 1_500.0,
        "_LIVE_AGGRESSIVE_MIN_MARKET_CAP_USD": 2_000.0,
        "_LIVE_AGGRESSIVE_MAX_MARKET_CAP_USD": 500_000.0,
        "_LIVE_AGGRESSIVE_MIN_SCORE_TOTAL": 30,
        "_LIVE_AGGRESSIVE_MIN_RANK_SCORE": 35.0,
        "_LIVE_AGGRESSIVE_MIN_TXNS_5M": 3,
        "_LIVE_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS": 5,
        "_LIVE_AGGRESSIVE_MAX_PRICE_IMPACT_PCT": 20.0,
        "_LIVE_AGGRESSIVE_REQUIRE_ROUTE": True,
        "_LIVE_AGGRESSIVE_REQUIRE_PRICE": True,
        "_LIVE_AGGRESSIVE_BUY_RESEARCH_LANES": True,
        "_PUMP_EARLY_SNIPER_ENABLED": False,
        "_PUMP_EARLY_SNIPER_MODE": "canary_aggressive",
        "_PUMP_EARLY_SNIPER_MIN_AGE_MIN": 3.0,
        "_PUMP_EARLY_SNIPER_MAX_AGE_MIN": 30.0,
        "_PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD": 2_000.0,
        "_PUMP_EARLY_SNIPER_MICRO_MIN_LIQUIDITY_USD": 1_000.0,
        "_PUMP_EARLY_SNIPER_MICRO_MIN_VOLUME_USD_24H": 30_000.0,
        "_PUMP_EARLY_SNIPER_MIN_MARKET_CAP_USD": 3_000.0,
        "_PUMP_EARLY_SNIPER_MAX_MARKET_CAP_USD": 200_000.0,
        "_PUMP_EARLY_SNIPER_MICRO_MAX_MARKET_CAP_USD": 125_000.0,
        "_PUMP_EARLY_SNIPER_MIN_SCORE_TOTAL": 35,
        "_PUMP_EARLY_SNIPER_MICRO_MIN_SCORE_TOTAL": 30,
        "_PUMP_EARLY_SNIPER_MIN_RANK_SCORE": 42.0,
        "_PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE": 45.0,
        "_PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT": 15.0,
        "_PUMP_EARLY_SNIPER_MICRO_MAX_PRICE_IMPACT_PCT": 12.0,
        "_PUMP_EARLY_SNIPER_MIN_TXNS_5M": 25,
        "_PUMP_EARLY_SNIPER_MICRO_MIN_TXNS_5M": 80,
        "_PUMP_EARLY_SNIPER_MIN_PRICE_PCT_5M": -12.0,
        "_PUMP_EARLY_SNIPER_MAX_PRICE_PCT_5M": 180.0,
        "_PUMP_EARLY_SNIPER_MICRO_MIN_PRICE_PCT_5M": 8.0,
        "_PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS": 4,
        "_PUMP_EARLY_SNIPER_HOT_MIN_RANK_SCORE": 50.0,
        "_PUMP_EARLY_SNIPER_HOT_MIN_TXNS_5M": 100,
        "_PUMP_EARLY_SNIPER_HOT_MIN_PRICE_PCT_5M": 10.0,
        "_PUMP_EARLY_SNIPER_HOT_MAX_PRICE_PCT_5M": 120.0,
        "_PUMP_EARLY_SNIPER_HOT_MAX_SNAPSHOT_MISSING_FIELDS": 2,
        "_PUMP_EARLY_PROFIT_LANE_ENABLED": False,
        "_PUMP_EARLY_PROFIT_DEX_ALLOWLIST": {"pumpswap"},
        "_PUMP_EARLY_PROFIT_REQUIRE_REAL_LIQUIDITY": True,
        "_PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD": 5_000.0,
        "_PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL": 35,
        "_PUMP_EARLY_PROFIT_MIN_AGE_MIN": 3.0,
        "_PUMP_EARLY_PROFIT_MAX_AGE_MIN": 30.0,
        "_PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT": 10.0,
        "_PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD": 0.0,
        "_PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD": 0.0,
        "_PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES": ((25.0, 999.0),),
        "_PUMP_EARLY_AGGRESSIVE_RESEARCH_GUARD_ENABLED": True,
        "_PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PRICE5M_RANGES": ((25.0, 999.0),),
        "_PUMP_EARLY_AGGRESSIVE_RESEARCH_DEX_ALLOWLIST": {"pumpswap"},
        "_PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_HIGH_MCAP_USD": 100_000.0,
        "_PUMP_EARLY_AGGRESSIVE_RESEARCH_HIGH_MCAP_ALLOW_MIN_TXNS_5M": 1_200,
        "_PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PROXY": True,
        "_PUMP_EARLY_METEOR_PRIME_ENABLED": False,
        "_PUMP_EARLY_METEOR_PRIME_MIN_LIQUIDITY_USD": 4_000.0,
        "_PUMP_EARLY_METEOR_PRIME_MAX_LIQUIDITY_USD": 30_000.0,
        "_PUMP_EARLY_METEOR_PRIME_MIN_MARKET_CAP_USD": 5_000.0,
        "_PUMP_EARLY_METEOR_PRIME_MAX_MARKET_CAP_USD": 30_000.0,
        "_PUMP_EARLY_METEOR_PRIME_MIN_PRICE_PCT_5M": 110.0,
        "_PUMP_EARLY_METEOR_PRIME_MAX_PRICE_PCT_5M": 300.0,
        "_PUMP_EARLY_METEOR_PRIME_MIN_TXNS_5M": 220,
        "_PUMP_EARLY_METEOR_PRIME_MIN_SCORE_TOTAL": 30,
        "_PUMP_EARLY_METEOR_PRIME_MIN_AGE_MIN": 3.0,
        "_PUMP_EARLY_METEOR_PRIME_MAX_AGE_MIN": 18.0,
        "_PUMP_EARLY_METEOR_PRIME_MAX_PRICE_IMPACT_PCT": 12.0,
        "_PUMP_EARLY_METEOR_PRIME_MIN_VOLUME_USD_24H": 8_000.0,
        "_PUMP_EARLY_BREAKOUT_PROBE_ENABLED": True,
        "_PUMP_EARLY_BREAKOUT_MIN_LIQUIDITY_USD": 5_000.0,
        "_PUMP_EARLY_BREAKOUT_MAX_LIQUIDITY_USD": 30_000.0,
        "_PUMP_EARLY_BREAKOUT_MIN_MARKET_CAP_USD": 5_000.0,
        "_PUMP_EARLY_BREAKOUT_MAX_MARKET_CAP_USD": 60_000.0,
        "_PUMP_EARLY_BREAKOUT_MIN_PRICE_PCT_5M": 25.0,
        "_PUMP_EARLY_BREAKOUT_MAX_PRICE_PCT_5M": 120.0,
        "_PUMP_EARLY_BREAKOUT_MIN_TXNS_5M": 300,
        "_PUMP_EARLY_BREAKOUT_MIN_VOLUME_USD_24H": 20_000.0,
        "_PUMP_EARLY_BREAKOUT_MIN_SCORE_TOTAL": 35,
        "_PUMP_EARLY_BREAKOUT_MIN_RANK_SCORE": 50.0,
        "_PUMP_EARLY_BREAKOUT_MIN_AGE_MIN": 2.0,
        "_PUMP_EARLY_BREAKOUT_MAX_AGE_MIN": 15.0,
        "_PUMP_EARLY_BREAKOUT_MAX_PRICE_IMPACT_PCT": 8.0,
        "_PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED": True,
        "_PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD": 200_000.0,
        "_PUMP_EARLY_PROFIT_DEEP_NEG_PRICE5M_PCT": -40.0,
        "_PUMP_EARLY_PROFIT_DEEP_NEG_MIN_TXNS_5M": 1_500,
        "_PUMP_EARLY_PROFIT_DEEP_NEG_MIN_VOLUME_USD_24H": 150_000.0,
        "_PUMP_EARLY_PROFIT_EXTREME_PRICE5M_PCT": 300.0,
        "_PUMP_EARLY_PROFIT_EXTREME_PRICE5M_MIN_MCAP_USD": 100_000.0,
        "_PUMP_EARLY_PROFIT_DEAD_VOLUME_MIN_USD_24H": 15_000.0,
        "_PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_USD_24H": 30_000.0,
        "_PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_TXNS_5M": 1_000,
        "_PUMP_EARLY_PROFIT_HOT_PRICE5M_MIN_PCT": 100.0,
        "_PUMP_EARLY_PROFIT_HOT_PRICE5M_MAX_PCT": 180.0,
        "_PUMP_EARLY_PROFIT_HOT_MCAP_MIN_USD": 50_000.0,
        "_PUMP_EARLY_PROFIT_HOT_MIN_LIQUIDITY_USD": 20_000.0,
        "_PUMP_EARLY_PROFIT_HOT_MIN_TXNS_5M": 600,
        "_PUMP_EARLY_PROFIT_HOT_MIN_VOLUME_USD_24H": 50_000.0,
        "_PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H": 0.0,
        "_PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_TXNS_5M": 500,
        "_PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_PRICE5M_PCT": 50.0,
        "_PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_TXNS_5M": 350,
        "_PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_VOLUME_USD_24H": 100_000.0,
        "_PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT": 40.0,
        "_PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT": 50.0,
        "_PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD": 100_000.0,
        "_PUMP_EARLY_QUALITY_MIN_POINTS": 0,
        "_PUMP_EARLY_QUALITY_MIN_AGE_MIN": 3.0,
        "_PUMP_EARLY_QUALITY_MIN_LIQUIDITY_USD": 8_000.0,
        "_PUMP_EARLY_QUALITY_MIN_VOLUME_USD_24H": 60_000.0,
        "_PUMP_EARLY_QUALITY_MIN_MARKET_CAP_USD": 20_000.0,
        "_PUMP_EARLY_QUALITY_MIN_HOLDERS": 15,
        "_PUMP_EARLY_QUALITY_MIN_SCORE_TOTAL": 50,
        "_PUMP_EARLY_QUALITY_MAX_PRICE_IMPACT_PCT": 8.0,
        "_DEX_MATURE_QUALITY_MIN_POINTS": 0,
        "_DEX_MATURE_QUALITY_MIN_AGE_MIN": 0.0,
        "_DEX_MATURE_QUALITY_MIN_LIQUIDITY_USD": 0.0,
        "_DEX_MATURE_QUALITY_MIN_VOLUME_USD_24H": 0.0,
        "_DEX_MATURE_QUALITY_MIN_MARKET_CAP_USD": 0.0,
        "_DEX_MATURE_QUALITY_MIN_HOLDERS": 0,
        "_DEX_MATURE_QUALITY_MIN_SCORE_TOTAL": 0,
    }
    namespace.update(overrides)
    exec(compile(module, "run_bot.py", "exec"), namespace)
    return namespace


def test_pump_live_profit_gate_blocks_token_below_hard_mins() -> None:
    gate = _load_entry_quality_gate()

    ok, reason = gate(
        {
            "age_min": 4.0,
            "liquidity_usd": 9_500.0,
            "score_total": 49,
            "market_cap_usd": 15_000.0,
            "snapshot_missing_fields": 0,
            "has_jupiter_route": 1,
            "require_jupiter_for_buy": 1,
        },
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 20.0},
    )

    assert ok is False
    assert reason.startswith("live_profit_gate:")
    assert "age<8" in reason
    assert "liq<10000" in reason
    assert "score<50" in reason
    assert "mcap<20000" in reason


def test_pump_live_profit_gate_rejects_when_rank_score_is_below_threshold() -> None:
    gate = _load_entry_quality_gate()

    ok, reason = gate(
        {
            "age_min": 9.0,
            "liquidity_usd": 12_000.0,
            "score_total": 55,
            "market_cap_usd": 40_000.0,
            "snapshot_missing_fields": 1,
            "price_impact_pct": 2.0,
            "has_jupiter_route": 1,
            "require_jupiter_for_buy": 1,
        },
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 12.0},
    )

    assert ok is False
    assert reason == "live_profit_gate:rank<12.5"


def test_pump_live_profit_gate_rejects_on_missing_fields_and_mcap_ceiling() -> None:
    gate = _load_entry_quality_gate()

    ok, reason = gate(
        {
            "age_min": 9.0,
            "liquidity_usd": 12_000.0,
            "score_total": 55,
            "market_cap_usd": 150_000.0,
            "snapshot_missing_fields": 5,
            "price_impact_pct": 2.0,
            "has_jupiter_route": 1,
            "require_jupiter_for_buy": 1,
        },
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 30.0},
    )

    assert ok is False
    assert reason.startswith("live_profit_gate:")
    assert "mcap>125000" in reason
    assert "missing>3" in reason


def test_pump_live_profit_gate_allows_token_that_meets_live_canary_gate() -> None:
    gate = _load_entry_quality_gate()

    ok, reason = gate(
        {
            "age_min": 9.0,
            "liquidity_usd": 12_000.0,
            "score_total": 55,
            "market_cap_usd": 40_000.0,
            "price_impact_pct": 2.0,
            "snapshot_missing_fields": 1,
            "has_jupiter_route": 1,
            "require_jupiter_for_buy": 1,
        },
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 30.0},
    )

    assert ok is True
    assert reason == ""


def test_pump_sniper_core_allows_lower_liquidity_momentum_entry() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True)
    token = {
        "age_min": 6.0,
        "liquidity_usd": 2_500.0,
        "volume_24h_usd": 22_000.0,
        "score_total": 35,
        "market_cap_usd": 18_000.0,
        "price_pct_5m": 18.0,
        "txns_last_5m": 35,
        "price_impact_pct": 9.0,
        "snapshot_missing_fields": 4,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 43.0})

    assert ok is True
    assert reason == ""
    assert token["entry_lane"] == "pump_early_sniper"
    assert token["sniper_gate_profile"] == "sniper_core"


def test_pump_sniper_micro_allows_score_30_with_strong_momentum() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True)
    token = {
        "age_min": 4.0,
        "liquidity_usd": 1_300.0,
        "volume_24h_usd": 45_000.0,
        "score_total": 30,
        "market_cap_usd": 12_000.0,
        "price_pct_5m": 22.0,
        "txns_last_5m": 95,
        "price_impact_pct": 8.0,
        "snapshot_missing_fields": 4,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 46.0})

    assert ok is True
    assert reason == ""
    assert token["sniper_gate_profile"] == "sniper_micro"


def test_pump_sniper_hot_tags_high_momentum_core_entry() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True)
    token = {
        "age_min": 8.0,
        "liquidity_usd": 12_000.0,
        "volume_24h_usd": 85_000.0,
        "score_total": 45,
        "market_cap_usd": 45_000.0,
        "price_pct_5m": 35.0,
        "txns_last_5m": 140,
        "price_impact_pct": 4.0,
        "snapshot_missing_fields": 1,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 55.0})

    assert ok is True
    assert reason == ""
    assert token["sniper_gate_profile"] == "sniper_hot"


def test_pump_sniper_rejects_when_neither_profile_matches() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True)
    token = {
        "age_min": 2.0,
        "liquidity_usd": 700.0,
        "volume_24h_usd": 5_000.0,
        "score_total": 25,
        "market_cap_usd": 1_500.0,
        "price_pct_5m": -30.0,
        "txns_last_5m": 10,
        "price_impact_pct": 20.0,
        "snapshot_missing_fields": 5,
        "has_jupiter_route": 0,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 20.0})

    assert ok is False
    assert reason.startswith("live_profit_gate:sniper_")
    assert token["entry_lane"] == "pump_early_reject"
    assert token["live_profit_gate_failed_count"] > 1


def test_pumpswap_profit_gate_allows_real_liquidity_bucket() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True, _PUMP_EARLY_PROFIT_LANE_ENABLED=True)
    token = {
        "dex_id": "pumpswap",
        "age_min": 6.0,
        "price_usd": 0.00001,
        "liquidity_usd": 12_000.0,
        "score_total": 38,
        "market_cap_usd": 18_000.0,
        "volume_24h_usd": 120_000.0,
        "price_pct_5m": 7.0,
        "txns_last_5m": 500,
        "price_impact_pct": 4.0,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 40.0})

    assert ok is True
    assert reason == ""
    assert token["entry_lane"] == "pump_early_pumpswap_profit"
    assert token["sniper_gate_profile"] == "pumpswap_profit_prime"
    assert token["profit_lane_tier"] == "pump_early_pumpswap_prime"


def test_pumpswap_profit_gate_blocks_proxy_liquidity_productive_lane() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True, _PUMP_EARLY_PROFIT_LANE_ENABLED=True)
    token = {
        "dex_id": "pumpswap",
        "age_min": 6.0,
        "price_usd": 0.00001,
        "liquidity_usd": 5_500.0,
        "liquidity_usd_is_proxy": 1,
        "score_total": 38,
        "market_cap_usd": 18_000.0,
        "price_pct_5m": 35.0,
        "price_impact_pct": 4.0,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 60.0})

    assert ok is False
    assert "liq_proxy" in reason
    assert token["entry_lane"] == "pump_early_sniper_research"
    assert token["blocked_bucket"] == "liquidity_proxy"


def test_pumpswap_profit_gate_blocks_toxic_price5m_bucket() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True, _PUMP_EARLY_PROFIT_LANE_ENABLED=True)
    token = {
        "dex_id": "pumpswap",
        "age_min": 6.0,
        "price_usd": 0.00001,
        "liquidity_usd": 8_500.0,
        "score_total": 38,
        "market_cap_usd": 32_000.0,
        "price_pct_5m": 75.0,
        "price_impact_pct": 4.0,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 60.0})

    assert ok is False
    assert "mcap_block" not in reason
    assert "price5m_25_999" in reason
    assert token["entry_lane"] == "pump_early_sniper_research"


def test_pumpswap_breakout_probe_allows_controlled_high_momentum() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True, _PUMP_EARLY_PROFIT_LANE_ENABLED=True)
    token = {
        "dex_id": "pumpswap",
        "age_min": 6.0,
        "price_usd": 0.00001,
        "liquidity_usd": 12_000.0,
        "score_total": 42,
        "market_cap_usd": 24_000.0,
        "volume_24h_usd": 90_000.0,
        "price_pct_5m": 72.0,
        "txns_last_5m": 650,
        "price_impact_pct": 5.0,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 55.0})

    assert ok is True
    assert reason == ""
    assert token["entry_lane"] == "pump_early_pumpswap_breakout_probe"
    assert token["sniper_gate_profile"] == "pumpswap_breakout_probe"
    assert token["profit_lane_tier"] == "pump_early_pumpswap_breakout_probe"
    assert "price5m_25_999" in token["breakout_standard_failures"]


def test_pumpswap_breakout_probe_rejects_high_mcap_local_top() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True, _PUMP_EARLY_PROFIT_LANE_ENABLED=True)
    token = {
        "dex_id": "pumpswap",
        "age_min": 6.0,
        "price_usd": 0.00001,
        "liquidity_usd": 18_000.0,
        "score_total": 42,
        "market_cap_usd": 95_000.0,
        "volume_24h_usd": 160_000.0,
        "price_pct_5m": 72.0,
        "txns_last_5m": 900,
        "price_impact_pct": 5.0,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 55.0})

    assert ok is False
    assert "price5m_25_999" in reason
    assert token["entry_lane"] == "pump_early_sniper_research"
    assert "breakout_mcap>60000" in token["breakout_gate_failures"]


def test_pumpswap_breakout_probe_requires_rank_and_volume() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True, _PUMP_EARLY_PROFIT_LANE_ENABLED=True)
    token = {
        "dex_id": "pumpswap",
        "age_min": 6.0,
        "price_usd": 0.00001,
        "liquidity_usd": 12_000.0,
        "score_total": 42,
        "market_cap_usd": 24_000.0,
        "volume_24h_usd": 10_000.0,
        "price_pct_5m": 72.0,
        "txns_last_5m": 650,
        "price_impact_pct": 5.0,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 45.0})

    assert ok is False
    assert "price5m_25_999" in reason
    assert token["entry_lane"] == "pump_early_sniper_research"
    assert "breakout_rank<50" in token["breakout_gate_failures"]
    assert "breakout_vol<20000" in token["breakout_gate_failures"]


def test_pumpswap_profit_shape_guard_blocks_deep_negative_without_support() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True, _PUMP_EARLY_PROFIT_LANE_ENABLED=True)
    token = {
        "dex_id": "pumpswap",
        "age_min": 9.0,
        "price_usd": 0.00001,
        "liquidity_usd": 6_341.0,
        "score_total": 45,
        "market_cap_usd": 6_719.0,
        "volume_24h_usd": 34_633.0,
        "price_pct_5m": -80.99,
        "txns_last_5m": 1_082,
        "price_impact_pct": 7.0,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 40.0})

    assert ok is False
    assert "shape_deep_negative_price5m" in reason
    assert token["entry_lane"] == "pump_early_sniper_research"
    assert token["blocked_bucket"] == "shape_deep_negative_price5m"


def test_pumpswap_profit_shape_guard_blocks_extreme_spike_high_mcap() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True, _PUMP_EARLY_PROFIT_LANE_ENABLED=True)
    token = {
        "dex_id": "pumpswap",
        "age_min": 10.0,
        "price_usd": 0.00001,
        "liquidity_usd": 120_719.0,
        "score_total": 45,
        "market_cap_usd": 2_401_581.0,
        "volume_24h_usd": 64_648.0,
        "price_pct_5m": 4_537.0,
        "txns_last_5m": 2_491,
        "price_impact_pct": 6.0,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 40.0})

    assert ok is False
    assert "shape_mcap>=200000" in reason
    assert "shape_extreme_price5m_mcap" in reason
    assert token["entry_lane"] == "pump_early_sniper_research"


def test_pumpswap_profit_shape_guard_blocks_low_volume_no_momentum() -> None:
    gate = _load_entry_quality_gate(
        _PUMP_EARLY_SNIPER_ENABLED=True,
        _PUMP_EARLY_PROFIT_LANE_ENABLED=True,
        _PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H=15_000.0,
    )
    token = {
        "dex_id": "pumpswap",
        "age_min": 7.0,
        "price_usd": 0.00001,
        "liquidity_usd": 12_050.0,
        "score_total": 45,
        "market_cap_usd": 24_718.0,
        "volume_24h_usd": 9_744.0,
        "price_pct_5m": -28.7,
        "txns_last_5m": 306,
        "price_impact_pct": 6.0,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 40.0})

    assert ok is False
    assert "shape_low_volume_no_momentum" in reason
    assert token["entry_lane"] == "pump_early_sniper_research"


def test_pumpswap_profit_shape_guard_blocks_weak_prime_mid_momentum() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True, _PUMP_EARLY_PROFIT_LANE_ENABLED=True)
    token = {
        "dex_id": "pumpswap",
        "age_min": 7.0,
        "price_usd": 0.00001,
        "liquidity_usd": 10_189.0,
        "score_total": 45,
        "market_cap_usd": 17_300.0,
        "volume_24h_usd": 74_955.0,
        "price_pct_5m": 32.71,
        "txns_last_5m": 259,
        "price_impact_pct": 6.0,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 40.0})

    assert ok is False
    assert "shape_prime_mid_momentum_weak" in reason
    assert token["entry_lane"] == "pump_early_sniper_research"


def test_pumpswap_profit_shape_guard_blocks_high_mcap_mid_momentum() -> None:
    gate = _load_entry_quality_gate(_PUMP_EARLY_SNIPER_ENABLED=True, _PUMP_EARLY_PROFIT_LANE_ENABLED=True)
    token = {
        "dex_id": "pumpswap",
        "age_min": 7.0,
        "price_usd": 0.00001,
        "liquidity_usd": 24_773.0,
        "score_total": 45,
        "market_cap_usd": 100_499.0,
        "volume_24h_usd": 129_814.0,
        "price_pct_5m": 48.61,
        "txns_last_5m": 1_321,
        "price_impact_pct": 6.0,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 40.0})

    assert ok is False
    assert "shape_high_mcap_mid_momentum" in reason
    assert token["entry_lane"] == "pump_early_sniper_research"


def test_pumpswap_meteor_prime_allows_high_velocity_low_mcap_breakout() -> None:
    gate = _load_entry_quality_gate(
        _PUMP_EARLY_SNIPER_ENABLED=True,
        _PUMP_EARLY_PROFIT_LANE_ENABLED=True,
        _PUMP_EARLY_METEOR_PRIME_ENABLED=True,
    )
    token = {
        "dex_id": "pumpswap",
        "age_min": 9.5,
        "price_usd": 0.00001,
        "liquidity_usd": 7_655.0,
        "score_total": 35,
        "market_cap_usd": 8_312.0,
        "volume_24h_usd": 12_725.0,
        "price_pct_5m": 166.0,
        "txns_last_5m": 315,
        "price_impact_pct": 7.9,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 40.0})

    assert ok is True
    assert reason == ""
    assert token["entry_lane"] == "pump_early_pumpswap_profit"
    assert token["sniper_gate_profile"] == "pumpswap_meteor_prime"
    assert token["profit_lane_tier"] == "pump_early_meteor_prime"


def test_pumpswap_meteor_prime_can_bypass_standard_score_and_impact_when_momentum_is_strong() -> None:
    gate = _load_entry_quality_gate(
        _PUMP_EARLY_SNIPER_ENABLED=True,
        _PUMP_EARLY_PROFIT_LANE_ENABLED=True,
        _PUMP_EARLY_METEOR_PRIME_ENABLED=True,
    )
    token = {
        "dex_id": "pumpswap",
        "age_min": 8.0,
        "price_usd": 0.00001,
        "liquidity_usd": 4_500.0,
        "score_total": 31,
        "market_cap_usd": 27_500.0,
        "volume_24h_usd": 20_000.0,
        "price_pct_5m": 180.0,
        "txns_last_5m": 350,
        "price_impact_pct": 11.0,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
    }

    ok, reason = gate(token, "pump_early", quality_points=0, rank_info={"rank_score": 40.0})

    assert ok is True
    assert reason == ""
    assert token["sniper_gate_profile"] == "pumpswap_meteor_prime"
    assert "score<35" in token["meteor_prime_standard_failures"]
    assert "impact>10" in token["meteor_prime_standard_failures"]


def test_pump_paper_cold_start_allows_relaxed_missing_and_score() -> None:
    gate = _load_entry_quality_gate(DRY_RUN=True, _stats={"sold": 0})

    ok, reason = gate(
        {
            "age_min": 12.5,
            "liquidity_usd": 10_500.0,
            "score_total": 45,
            "market_cap_usd": 16_000.0,
            "price_pct_5m": 35.0,
            "price_impact_pct": 2.0,
            "snapshot_missing_fields": 4,
            "has_jupiter_route": 1,
            "require_jupiter_for_buy": 1,
        },
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 30.0},
    )

    assert ok is True
    assert reason == ""


def test_pump_paper_cold_start_expires_after_closed_trade_cap() -> None:
    gate = _load_entry_quality_gate(DRY_RUN=True, _stats={"sold": 50})

    ok, reason = gate(
        {
            "age_min": 5.5,
            "liquidity_usd": 10_500.0,
            "score_total": 45,
            "market_cap_usd": 16_000.0,
            "price_pct_5m": 35.0,
            "price_impact_pct": 2.0,
            "snapshot_missing_fields": 4,
            "has_jupiter_route": 1,
            "require_jupiter_for_buy": 1,
        },
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 30.0},
    )

    assert ok is False
    assert reason.startswith("live_profit_gate:")
    assert "age<8" in reason
    assert "score<50" in reason
    assert "mcap<20000" in reason
    assert "missing>3" in reason


def test_pump_paper_cold_start_rejects_negative_momentum() -> None:
    gate = _load_entry_quality_gate(DRY_RUN=True, _stats={"sold": 0})

    ok, reason = gate(
        {
            "age_min": 12.5,
            "liquidity_usd": 13_000.0,
            "score_total": 45,
            "market_cap_usd": 29_000.0,
            "price_pct_5m": -18.6,
            "price_impact_pct": 2.0,
            "snapshot_missing_fields": 4,
            "has_jupiter_route": 1,
            "require_jupiter_for_buy": 1,
        },
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 30.0},
    )

    assert ok is False
    assert reason == "live_profit_gate:price5m<0"


def test_pump_paper_cold_start_rejects_overheated_momentum() -> None:
    gate = _load_entry_quality_gate(DRY_RUN=True, _stats={"sold": 0})

    ok, reason = gate(
        {
            "age_min": 24.0,
            "liquidity_usd": 13_000.0,
            "score_total": 45,
            "market_cap_usd": 29_000.0,
            "price_pct_5m": 103.0,
            "price_impact_pct": 2.0,
            "snapshot_missing_fields": 4,
            "has_jupiter_route": 1,
            "require_jupiter_for_buy": 1,
        },
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 30.0},
    )

    assert ok is False
    assert reason == "live_profit_gate:price5m>80"


def test_pump_paper_cold_start_requires_momentum_snapshot() -> None:
    gate = _load_entry_quality_gate(DRY_RUN=True, _stats={"sold": 0})

    ok, reason = gate(
        {
            "age_min": 12.5,
            "liquidity_usd": 13_000.0,
            "score_total": 45,
            "market_cap_usd": 29_000.0,
            "price_impact_pct": 2.0,
            "snapshot_missing_fields": 4,
            "has_jupiter_route": 1,
            "require_jupiter_for_buy": 1,
        },
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 30.0},
    )

    assert ok is False
    assert reason == "live_profit_gate:price5m_missing"


def test_pump_paper_cold_start_shadow_probe_allows_recovery_entry() -> None:
    namespace = _load_quality_namespace(DRY_RUN=True, _stats={"sold": 3})
    allowed = namespace["_paper_cold_start_shadow_probe_allowed"](
        SimpleNamespace(action="shadow", requested_mode="live", reason="recovery_not_ready"),
        SimpleNamespace(regime="pump_early"),
        True,
        3,
    )

    assert allowed is True


def test_pump_quality_points_still_apply_after_live_profit_gate() -> None:
    gate = _load_entry_quality_gate(
        _PUMP_EARLY_QUALITY_MIN_POINTS=5,
        _PUMP_EARLY_QUALITY_MAX_PRICE_IMPACT_PCT=0.0,
    )

    ok, reason = gate(
        {
            "age_min": 9.0,
            "liquidity_usd": 12_000.0,
            "score_total": 55,
            "market_cap_usd": 40_000.0,
            "price_impact_pct": 2.0,
            "snapshot_missing_fields": 1,
            "has_jupiter_route": 1,
            "require_jupiter_for_buy": 1,
        },
        "pump_early",
        quality_points=0,
        rank_info={"rank_score": 30.0},
    )

    assert ok is False
    assert reason.startswith("pump_quality=")


def test_paper_aggressive_buys_research_lane_in_dry_run() -> None:
    gate = _load_entry_quality_gate(
        DRY_RUN=True,
        _PUMP_EARLY_SNIPER_ENABLED=True,
        _PUMP_EARLY_PROFIT_LANE_ENABLED=True,
        _PAPER_AGGRESSIVE_TRADING_ENABLED=True,
    )
    token = {
        "age_min": 0.5,
        "liquidity_usd": 7_000.0,
        "score_total": 45,
        "market_cap_usd": 35_000.0,
        "price_usd": 0.00001,
        "price_pct_5m": 7.0,
        "price_impact_pct": 2.0,
        "snapshot_missing_fields": 3,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
        "dex_id": "pumpswap",
        "txns_last_5m": 25,
    }

    ok, reason = gate(
        token,
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 45.0},
    )

    assert ok is True
    assert reason == ""
    assert token["gate_profile"] == "paper_aggressive_research_buy"
    assert token["entry_lane"] == "pump_early_sniper_research"


def test_paper_aggressive_blocks_toxic_price5m_bucket() -> None:
    gate = _load_entry_quality_gate(
        DRY_RUN=True,
        _PUMP_EARLY_SNIPER_ENABLED=True,
        _PUMP_EARLY_PROFIT_LANE_ENABLED=True,
        _PAPER_AGGRESSIVE_TRADING_ENABLED=True,
    )
    token = {
        "age_min": 8.0,
        "liquidity_usd": 7_000.0,
        "score_total": 45,
        "market_cap_usd": 35_000.0,
        "price_usd": 0.00001,
        "price_pct_5m": 72.0,
        "price_impact_pct": 2.0,
        "snapshot_missing_fields": 3,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
        "dex_id": "pumpswap",
        "txns_last_5m": 200,
    }

    ok, reason = gate(
        token,
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 45.0},
    )

    assert ok is False
    assert "research_price5m_25_999" in reason
    assert token["gate_profile"] == "paper_aggressive_research_guard"


def test_paper_aggressive_allows_high_mcap_with_low_momentum() -> None:
    gate = _load_entry_quality_gate(
        DRY_RUN=True,
        _PUMP_EARLY_SNIPER_ENABLED=True,
        _PUMP_EARLY_PROFIT_LANE_ENABLED=True,
        _PAPER_AGGRESSIVE_TRADING_ENABLED=True,
    )
    token = {
        "age_min": 8.0,
        "liquidity_usd": 32_000.0,
        "score_total": 45,
        "market_cap_usd": 150_000.0,
        "price_usd": 0.00001,
        "price_pct_5m": 7.0,
        "price_impact_pct": 2.0,
        "snapshot_missing_fields": 3,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
        "dex_id": "pumpswap",
        "txns_last_5m": 1_502,
    }

    ok, reason = gate(
        token,
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 45.0},
    )

    assert ok is True
    assert reason == ""
    assert token["gate_profile"] == "pumpswap_profit_broad"
    assert token["entry_lane"] == "pump_early_pumpswap_profit"


def test_paper_aggressive_blocks_unsupported_high_mcap() -> None:
    gate = _load_entry_quality_gate(
        DRY_RUN=True,
        _PUMP_EARLY_SNIPER_ENABLED=True,
        _PUMP_EARLY_PROFIT_LANE_ENABLED=True,
        _PAPER_AGGRESSIVE_TRADING_ENABLED=True,
    )
    token = {
        "age_min": 8.0,
        "liquidity_usd": 20_000.0,
        "score_total": 45,
        "market_cap_usd": 250_000.0,
        "price_usd": 0.00001,
        "price_pct_5m": -15.0,
        "price_impact_pct": 2.0,
        "snapshot_missing_fields": 3,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
        "dex_id": "pumpswap",
        "txns_last_5m": 200,
    }

    ok, reason = gate(
        token,
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 45.0},
    )

    assert ok is False
    assert "research_mcap>=100000" in reason


def test_live_aggressive_buys_research_lane_in_live_mode() -> None:
    gate = _load_entry_quality_gate(
        DRY_RUN=False,
        _PUMP_EARLY_SNIPER_ENABLED=True,
        _PUMP_EARLY_PROFIT_LANE_ENABLED=True,
        _LIVE_AGGRESSIVE_TRADING_ENABLED=True,
    )
    token = {
        "age_min": 0.2,
        "liquidity_usd": 7_000.0,
        "score_total": 45,
        "market_cap_usd": 35_000.0,
        "price_usd": 0.00001,
        "price_pct_5m": 7.0,
        "price_impact_pct": 2.0,
        "snapshot_missing_fields": 3,
        "has_jupiter_route": 1,
        "require_jupiter_for_buy": 1,
        "dex_id": "pumpswap",
        "txns_last_5m": 25,
    }

    ok, reason = gate(
        token,
        "pump_early",
        quality_points=5,
        rank_info={"rank_score": 45.0},
    )

    assert ok is True
    assert reason == ""
    assert token["gate_profile"] == "live_aggressive_research_buy"
    assert token["entry_lane"] == "pump_early_sniper_research"
