from __future__ import annotations

from hashlib import sha256
from typing import Iterable

from features.builder import ALLOWED_FEATURES

_COMMON = [
    "entry_regime_code",
    "dex_id_code",
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
    "price5m_bucket_code",
    "green_sniper_score",
    "volume_pct_5m",
    "price_impact_pct",
    "impact_zero_flag",
    "social_ok",
    "social_link_count",
    "social_confidence_bonus",
    "twitter_followers",
    "discord_members",
    "score_total",
    "trend",
    "has_jupiter_route",
    "require_jupiter_for_buy",
    "route_proxy",
    "liquidity_is_proxy",
    "venue_is_pumpswap",
    "mcap_bucket_code",
    "missing_liquidity",
    "missing_volume",
    "missing_holders",
    "missing_rug_score",
    "missing_socials",
    "missing_trend",
]

FEATURE_SETS: dict[str, list[str]] = {
    "green_sniper_features": _COMMON + ["green_sniper_paper_birth_probe"],
    "late_momentum_features": _COMMON + ["price_pct_5m", "txns_last_5m", "price_impact_pct"],
    "research_rank_features": _COMMON + ["green_sniper_score"],
    "risk_features": _COMMON,
    "ev_features": _COMMON,
    "runner_features": _COMMON + ["green_sniper_score"],
    "continuation_features": _COMMON + ["price_pct_5m", "txns_last_5m", "price_impact_pct", "route_proxy"],
    "exit_features": _COMMON + ["exit_profile"],
}

_FORBIDDEN_SUBSTR = (
    "future",
    "close_price",
    "_at_close",
    "_after_",
    "outcome",
    "result",
    "realized",
    "pnl_seen",
    "max_pnl",
    "peak_pnl",
)
_SAFE = {"exit_profile"}


def validate_feature_set(features: Iterable[str]) -> list[str]:
    bad: list[str] = []
    for feature in features:
        low = feature.lower()
        if feature not in _SAFE and any(item in low for item in _FORBIDDEN_SUBSTR):
            bad.append(feature)
        if feature not in ALLOWED_FEATURES and feature not in {"label", "target_total_pnl_pct"}:
            bad.append(feature)
    return sorted(set(bad))


def feature_set(name: str) -> list[str]:
    if name not in FEATURE_SETS:
        raise KeyError(f"unknown feature set: {name}")
    bad = validate_feature_set(FEATURE_SETS[name])
    if bad:
        raise ValueError(f"feature set {name} has forbidden/non-T0 features: {bad}")
    return list(FEATURE_SETS[name])


def feature_set_hash(name: str) -> str:
    payload = "\n".join(feature_set(name)).encode("utf-8")
    return sha256(payload).hexdigest()[:16]


__all__ = ["FEATURE_SETS", "feature_set", "feature_set_hash", "validate_feature_set"]
