from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterable

import numpy as np
import pandas as pd

from analytics.social_signal import social_signal_from_token
from ml.data_contract import (
    normalize_dex_id as contract_normalize_dex_id,
    normalize_entry_regime as contract_normalize_entry_regime,
    normalize_price_source,
    reconstruct_entry_lane,
)
from utils.data_utils import (
    is_incomplete as token_is_incomplete,
    is_missing_value,
    sanitize_token_data,
)
from utils.time import utc_now

COLUMNS: list[str] = [
    "address",
    "timestamp",
    "discovered_via",
    "discovered_via_code",
    "entry_regime",
    "entry_regime_code",
    "entry_lane",
    "gate_profile",
    "profit_lane_tier",
    "dex_id",
    "dex_id_code",
    "price_source",
    "price_source_quality",
    "age_minutes",
    "queue_attempts",
    "queue_age_minutes",
    "snapshot_missing_fields",
    "coverage_core_fields",
    "liquidity_usd",
    "volume_24h_usd",
    "market_cap_usd",
    "txns_last_5m",
    "txns_last_5m_buys",
    "txns_last_5m_sells",
    "holders",
    "rug_score",
    "cluster_bad",
    "mint_auth_renounced",
    "price_pct_1m",
    "price_pct_5m",
    "price5m_bucket",
    "price5m_bucket_code",
    "green_sniper_score",
    "green_sniper_action",
    "green_sniper_reason",
    "green_sniper_paper_birth_probe",
    "profit_pnl_guard_failures",
    "volume_pct_5m",
    "price_impact_pct",
    "impact_zero_flag",
    "social_ok",
    "social_status",
    "twitter_present",
    "telegram_present",
    "discord_present",
    "website_present",
    "social_link_count",
    "social_confidence_bonus",
    "social_risk_flags",
    "social_latency_ms",
    "twitter_followers",
    "discord_members",
    "score_total",
    "trend",
    "has_jupiter_route",
    "require_jupiter_for_buy",
    "liquidity_is_proxy",
    "venue_is_pumpswap",
    "mcap_bucket",
    "mcap_bucket_code",
    "missing_liquidity",
    "missing_volume",
    "missing_holders",
    "missing_rug_score",
    "missing_socials",
    "missing_trend",
    "strategy_version",
    "experiment_id",
    "exit_profile",
    "config_hash",
    "is_incomplete",
]

_BOOL_COLS = {
    "cluster_bad",
    "mint_auth_renounced",
    "social_ok",
    "twitter_present",
    "telegram_present",
    "discord_present",
    "website_present",
    "has_jupiter_route",
    "require_jupiter_for_buy",
    "liquidity_is_proxy",
    "venue_is_pumpswap",
    "impact_zero_flag",
}

_DISCOVERY_CODE = {
    "dex": 0,
    "pumpfun": 1,
    "revival": 2,
}

_ENTRY_REGIME_CODE = {
    "dex_mature": 0,
    "pump_early": 1,
    "revival": 2,
}

_DEX_ID_CODE = {
    "unknown": 0,
    "pumpswap": 1,
    "pumpfun": 2,
    "meteora": 3,
    "raydium": 4,
    "orca": 5,
}

_PRICE_SOURCE_QUALITY = {
    "unknown": 0,
    "sol_estimate": 1,
    "dexscreener": 2,
    "dex_full": 2,
    "geckoterminal": 2,
    "birdeye": 3,
    "jupiter": 4,
    "jup_batch": 4,
    "jup_single": 4,
    "jup_critical": 4,
}

_COVERAGE_FIELDS: tuple[str, ...] = (
    "liquidity_usd",
    "volume_24h_usd",
    "holders",
    "rug_score",
    "social_ok",
    "trend",
    "txns_last_5m",
)

# Control conceptual T0
ALLOWED_FEATURES: set[str] = {
    "discovered_via",
    "discovered_via_code",
    "entry_regime",
    "entry_regime_code",
    "entry_lane",
    "gate_profile",
    "profit_lane_tier",
    "dex_id",
    "dex_id_code",
    "price_source",
    "price_source_quality",
    "age_minutes",
    "queue_attempts",
    "queue_age_minutes",
    "snapshot_missing_fields",
    "coverage_core_fields",
    "liquidity_usd",
    "volume_24h_usd",
    "market_cap_usd",
    "txns_last_5m",
    "txns_last_5m_buys",
    "txns_last_5m_sells",
    "holders",
    "rug_score",
    "cluster_bad",
    "mint_auth_renounced",
    "price_pct_1m",
    "price_pct_5m",
    "price5m_bucket",
    "price5m_bucket_code",
    "green_sniper_score",
    "green_sniper_action",
    "green_sniper_reason",
    "green_sniper_paper_birth_probe",
    "profit_pnl_guard_failures",
    "volume_pct_5m",
    "price_impact_pct",
    "impact_zero_flag",
    "social_ok",
    "social_status",
    "twitter_present",
    "telegram_present",
    "discord_present",
    "website_present",
    "social_link_count",
    "social_confidence_bonus",
    "social_risk_flags",
    "social_latency_ms",
    "twitter_followers",
    "discord_members",
    "score_total",
    "trend",
    "has_jupiter_route",
    "require_jupiter_for_buy",
    "liquidity_is_proxy",
    "venue_is_pumpswap",
    "mcap_bucket",
    "mcap_bucket_code",
    "missing_liquidity",
    "missing_volume",
    "missing_holders",
    "missing_rug_score",
    "missing_socials",
    "missing_trend",
    "strategy_version",
    "experiment_id",
    "exit_profile",
    "config_hash",
}
FORBIDDEN_FEATURES: set[str] = set()
_FORBIDDEN_SUBSTR: tuple[str, ...] = (
    "pnl",
    "future",
    "close_price",
    "_at_close",
    "_after_",
    "outcome",
    "result",
    "exit",
    "tp_",
    "sl_",
)

_SAFE_T0_PREFIXES: tuple[str, ...] = ("txns_last_",)
_SAFE_T0_METADATA_KEYS: set[str] = {
    "exit_profile",
    "profit_pnl_guard_failures",
    "runner_exit_profile",
}


def _has_forbidden_keys(
    d: Dict[str, Any],
    forbidden_exact: Iterable[str],
    forbidden_substr: Iterable[str],
) -> list[str]:
    keys = []
    for k in d.keys():
        lk = str(k).lower()
        if lk.startswith(_SAFE_T0_PREFIXES):
            continue
        if lk in _SAFE_T0_METADATA_KEYS:
            continue
        if k in forbidden_exact:
            keys.append(k)
            continue
        if any(sub in lk for sub in forbidden_substr):
            keys.append(k)
    return keys


def _normalize_discovery(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"pumpfun", "pump", "pump_fun"}:
        return "pumpfun"
    if raw in {"revival", "revive", "revived"}:
        return "revival"
    return "dex"


def _normalize_entry_regime(value: Any) -> str:
    return contract_normalize_entry_regime(value)


def _normalize_dex_id(value: Any) -> str:
    return contract_normalize_dex_id(value)


def _mcap_bucket(value: Any) -> tuple[str, int]:
    try:
        mcap = float(value)
    except Exception:
        return "missing", 0
    if mcap <= 0:
        return "missing", 0
    if mcap < 25_000:
        return "<25k", 1
    if mcap < 50_000:
        return "25k_50k", 2
    if mcap < 100_000:
        return "50k_100k", 3
    if mcap < 200_000:
        return "100k_200k", 4
    return ">=200k", 5


def _price5m_bucket(value: Any) -> tuple[str, int]:
    try:
        price5m = float(value)
    except Exception:
        return "missing", 0
    if price5m < 0:
        return "<0", 1
    if price5m < 25:
        return "0_25", 2
    if price5m < 50:
        return "25_50", 3
    if price5m < 100:
        return "50_100", 4
    if price5m < 180:
        return "100_180", 5
    return ">=180", 6


def _as_bool_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value != 0)
    raw = str(value or "").strip().lower()
    return int(raw in {"1", "true", "yes", "y", "on"})


def _price_source_quality(source: Any) -> int:
    key = str(source or "unknown").strip().lower()
    return int(_PRICE_SOURCE_QUALITY.get(key, 0))


def _missing_flags(tok: Dict[str, Any]) -> Dict[str, int]:
    return {
        "missing_liquidity": int(is_missing_value(tok.get("liquidity_usd"), treat_zero_as_missing=True)),
        "missing_volume": int(is_missing_value(tok.get("volume_24h_usd"), treat_zero_as_missing=True)),
        "missing_holders": int(is_missing_value(tok.get("holders"), treat_zero_as_missing=True)),
        "missing_rug_score": int(is_missing_value(tok.get("rug_score"))),
        "missing_socials": int(is_missing_value(tok.get("social_ok"))),
        "missing_trend": int(is_missing_value(tok.get("trend"))),
    }


def _coverage_metrics(tok: Dict[str, Any], missing_flags: Dict[str, int]) -> Dict[str, int]:
    missing = 0

    def _is_missing_core(field: str) -> bool:
        if field == "liquidity_usd":
            return bool(missing_flags["missing_liquidity"])
        if field == "volume_24h_usd":
            return bool(missing_flags["missing_volume"])
        if field == "holders":
            return bool(missing_flags["missing_holders"])
        if field == "rug_score":
            return bool(missing_flags["missing_rug_score"])
        if field == "social_ok":
            return bool(missing_flags["missing_socials"])
        if field == "trend":
            return bool(missing_flags["missing_trend"])
        return bool(is_missing_value(tok.get(field)))

    for field in _COVERAGE_FIELDS:
        if _is_missing_core(field):
            missing += 1

    total = len(_COVERAGE_FIELDS)
    return {
        "snapshot_missing_fields": int(missing),
        "coverage_core_fields": int(max(0, total - missing)),
    }


def _feature_value(tok: Dict[str, Any], col: str) -> Any:
    val = tok.get(col, None)
    if col in _BOOL_COLS:
        if is_missing_value(val):
            return np.nan
        return int(bool(val))
    if is_missing_value(val):
        return np.nan
    return val


def _coerce_age_minutes(tok: Dict[str, Any], now: dt.datetime) -> float:
    created_at = tok.get("created_at")
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (now - created_at).total_seconds() / 60.0)

    for key in ("age_min", "age_minutes", "queue_age_minutes"):
        raw = tok.get(key)
        try:
            if raw is None:
                continue
            return max(0.0, float(raw))
        except Exception:
            continue
    return 0.0


def build_feature_vector(tok: Dict[str, Any]) -> pd.Series:
    tok = sanitize_token_data(dict(tok))

    bad_keys = _has_forbidden_keys(tok, FORBIDDEN_FEATURES, _FORBIDDEN_SUBSTR)
    assert not bad_keys, f"Token incluye claves de futuro/no permitidas: {bad_keys}"

    now = utc_now()
    age_min = _coerce_age_minutes(tok, now)

    discovered_via = _normalize_discovery(tok.get("discovered_via", "dex"))
    entry_regime = _normalize_entry_regime(tok.get("entry_regime") or tok.get("discovered_via"))
    dex_id = _normalize_dex_id(tok.get("dex_id") or tok.get("dexId"))
    entry_lane = reconstruct_entry_lane(tok)
    mcap_bucket, mcap_bucket_code = _mcap_bucket(tok.get("market_cap_usd"))
    price5m_bucket, price5m_bucket_code = _price5m_bucket(tok.get("price_pct_5m"))
    missing_flags = _missing_flags(tok)
    coverage = _coverage_metrics(tok, missing_flags)
    social = social_signal_from_token(tok)

    values: Dict[str, Any] = {
        "address": tok.get("address"),
        "timestamp": now,
        "discovered_via": discovered_via,
        "discovered_via_code": int(_DISCOVERY_CODE.get(discovered_via, 0)),
        "entry_regime": entry_regime,
        "entry_regime_code": int(_ENTRY_REGIME_CODE.get(entry_regime, 0)),
        "entry_lane": entry_lane,
        "gate_profile": str(tok.get("gate_profile") or tok.get("sniper_gate_profile") or "").strip() or None,
        "profit_lane_tier": str(tok.get("profit_lane_tier") or "").strip() or None,
        "dex_id": dex_id,
        "dex_id_code": int(_DEX_ID_CODE.get(dex_id, 0)),
        "price_source": normalize_price_source(tok.get("price_source")),
        "price_source_quality": _price_source_quality(tok.get("price_source")),
        "age_minutes": age_min,
        "liquidity_is_proxy": _as_bool_int(tok.get("liquidity_is_proxy") or tok.get("liquidity_usd_is_proxy")),
        "venue_is_pumpswap": int(dex_id == "pumpswap"),
        "mcap_bucket": str(tok.get("mcap_bucket") or mcap_bucket),
        "mcap_bucket_code": int(mcap_bucket_code),
        "price5m_bucket": str(tok.get("price5m_bucket") or price5m_bucket),
        "price5m_bucket_code": int(price5m_bucket_code),
        "green_sniper_score": tok.get("green_sniper_score"),
        "green_sniper_action": tok.get("green_sniper_action"),
        "green_sniper_reason": tok.get("green_sniper_reason"),
        "green_sniper_paper_birth_probe": _as_bool_int(tok.get("green_sniper_paper_birth_probe")),
        "profit_pnl_guard_failures": tok.get("profit_pnl_guard_failures"),
        "impact_zero_flag": int(float(tok.get("price_impact_pct") or 0.0) == 0.0),
        "social_status": social.status,
        "social_ok": social.social_ok,
        "twitter_present": int(social.twitter_present),
        "telegram_present": int(social.telegram_present),
        "discord_present": int(social.discord_present),
        "website_present": int(social.website_present),
        "social_link_count": int(social.link_count),
        "social_confidence_bonus": float(social.confidence_bonus),
        "social_risk_flags": ",".join(social.risk_flags),
        "social_latency_ms": social.latency_ms,
        "strategy_version": tok.get("strategy_version"),
        "experiment_id": tok.get("experiment_id"),
        "exit_profile": tok.get("exit_profile") or tok.get("runner_exit_profile"),
        "config_hash": tok.get("config_hash"),
    }
    values.update(missing_flags)
    values.update(coverage)

    for col in COLUMNS:
        if col in values or col == "is_incomplete":
            continue
        values[col] = _feature_value(tok, col)

    values["is_incomplete"] = int(token_is_incomplete(tok))

    return pd.Series([values.get(c, np.nan) for c in COLUMNS], index=COLUMNS)
