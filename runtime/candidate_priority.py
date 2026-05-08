from __future__ import annotations

import datetime as dt
from typing import Any

from config.config import CFG
from ml.lane_taxonomy import LANE_RESEARCH_RANK_CANARY, LANE_RESEARCH_SNIPER, normalize_entry_lane


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        out = float(value)
        if out != out:
            return float(default)
        return out
    except Exception:
        return float(default)


def _source_score(source: str) -> float:
    raw = str(source or "").strip().lower()
    if raw in {"pumpportal", "pump_portal"}:
        return 35.0
    if raw in {"pumpfun", "pump_fun", "pump"}:
        return 30.0
    return 0.0


def _age_minutes(token: dict[str, Any], now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now(dt.timezone.utc)
    created = token.get("created_at") or token.get("createdAt")
    if isinstance(created, str):
        try:
            created = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        except Exception:
            created = None
    if isinstance(created, dt.datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (now - created).total_seconds() / 60.0)
    for key in ("age_minutes", "age_min", "queue_age_minutes"):
        if token.get(key) is not None:
            return _to_float(token.get(key), 0.0)
    return 0.0


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _score_threshold(value: Any, default: float = 0.647) -> float:
    return _normalize_score(value, default)


def _normalize_score(value: Any, default: float = 0.0) -> float:
    raw = _to_float(value, default)
    return raw * 100.0 if 0.0 < raw <= 1.0 else raw


def research_rank_priority_fit(token: dict[str, Any]) -> bool:
    lane = normalize_entry_lane(token.get("entry_lane") or token.get("profit_lane_tier"))
    rank = _normalize_score(token.get("rank_score") or token.get("research_rank_score"), -1.0)
    price5m = _to_float(token.get("price_pct_5m") or token.get("buy_price_pct_5m"), 0.0)
    txns5m = _to_float(token.get("txns_last_5m") or token.get("buy_txns_last_5m"), 0.0)
    mcap = _to_float(token.get("market_cap_usd") or token.get("buy_market_cap_usd"), 0.0)
    proxy = _boolish(token.get("liquidity_is_proxy") or token.get("liquidity_usd_is_proxy") or token.get("buy_liquidity_is_proxy"))
    has_route = _boolish(token.get("has_jupiter_route"))
    if lane not in {LANE_RESEARCH_SNIPER, LANE_RESEARCH_RANK_CANARY}:
        return False
    if rank < _score_threshold(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_SCORE", 0.647), 0.647):
        return False
    if txns5m < _to_float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_TXNS_5M", 300), 300.0):
        return False
    if not (
        _to_float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_MCAP_USD", 25_000.0), 25_000.0)
        <= mcap
        <= _to_float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_MCAP_USD", 100_000.0), 100_000.0)
    ):
        return False
    if not (
        _to_float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_PRICE5M", 25.0), 25.0)
        <= price5m
        <= _to_float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_PRICE5M", 100.0), 100.0)
    ):
        return False
    if proxy and bool(getattr(CFG, "RESEARCH_RANK_CANARY_PREFER_REAL_LIQUIDITY", True)):
        return False
    if bool(getattr(CFG, "RESEARCH_RANK_CANARY_REQUIRE_ROUTE_PAPER", True)) and not has_route:
        return False
    return True


def candidate_priority_score(token: dict[str, Any], *, source: str | None = None, now: dt.datetime | None = None) -> float:
    src = source or token.get("source") or token.get("discovered_via")
    age_min = _age_minutes(token, now)
    price5m = _to_float(token.get("price_pct_5m"), 0.0)
    txns5m = _to_float(token.get("txns_last_5m"), 0.0)
    liq = _to_float(token.get("liquidity_usd"), 0.0)
    rank = _normalize_score(token.get("rank_score") or token.get("research_rank_score"), -1.0)
    score = _source_score(str(src))
    score += max(0.0, 20.0 - age_min) * 1.5
    if rank >= 75:
        score += 35.0
    elif rank >= 61:
        score += 25.0
    elif rank >= 50:
        score += 12.0
    elif 0 <= rank < 35:
        score -= 12.0
    score += min(max(price5m, 0.0), 180.0) / 6.0
    score += min(txns5m, 180.0) / 4.0
    score += min(liq / 500.0, 15.0)
    if research_rank_priority_fit(token):
        score += _to_float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_BONUS", 25.0), 25.0)
    return round(max(0.0, score), 3)


__all__ = ["candidate_priority_score", "research_rank_priority_fit"]
