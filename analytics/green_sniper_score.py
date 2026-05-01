from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analytics.token_time import compute_age_minutes


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if out != out:
            return default
        return out
    except Exception:
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class GreenScoreBreakdown:
    score: float
    rank_component: float
    momentum_component: float
    liquidity_quality_component: float
    route_component: float
    age_component: float
    risk_penalty: float
    proxy_penalty: float
    extreme_momentum_penalty: float
    missing_data_penalty: float

    def to_dict(self) -> dict[str, float]:
        return self.__dict__.copy()


def momentum_component(price5m: float) -> tuple[float, float]:
    if price5m < 0:
        return 0.0, 8.0
    if price5m < 25:
        return 5.0, 0.0
    if price5m < 50:
        return 12.0, 0.0
    if price5m < 100:
        return 24.0, 0.0
    if price5m < 180:
        return 16.0, 8.0
    if price5m < 300:
        return 8.0, 18.0
    return 0.0, 35.0


def score_green_sniper(token: dict[str, Any], *, has_route: bool, proxy_liquidity: bool, live: bool) -> GreenScoreBreakdown:
    price5m = _float(token.get("price_pct_5m"), 0.0)
    rank = _float(token.get("rank_score") or token.get("research_rank_score"), -1.0)
    liq = _float(token.get("liquidity_usd"), 0.0)
    txns = _float(token.get("txns_last_5m"), 0.0)
    age = compute_age_minutes(token)
    impact = _float(token.get("price_impact_pct"), 0.0)
    mcap = _float(token.get("market_cap_usd"), 0.0)

    if rank >= 75:
        rank_component = 30.0
    elif rank >= 61:
        rank_component = 24.0
    elif rank >= 50:
        rank_component = 14.0
    elif rank >= 35:
        rank_component = 5.0
    elif rank >= 0:
        rank_component = -10.0
    else:
        rank_component = 0.0

    mom_component, extreme_penalty = momentum_component(price5m)
    momentum_txns_bonus = min(txns, 250.0) / 12.5
    liquidity_quality = min(max(liq - 1000.0, 0.0) / 350.0, 20.0)
    if liq >= 2500 and not proxy_liquidity:
        liquidity_quality += 6.0
    route_component = 10.0 if has_route else (0.0 if live else 3.0)
    if age <= 1.5:
        age_component = 12.0
    elif age <= 4:
        age_component = 8.0
    elif age <= 8:
        age_component = 3.0
    else:
        age_component = -8.0

    risk_penalty = 0.0
    if impact > 12:
        risk_penalty += min(impact - 12.0, 25.0)
    if mcap > 180_000:
        risk_penalty += 20.0
    if mcap <= 0:
        risk_penalty += 8.0
    proxy_penalty = 18.0 if proxy_liquidity and live else (8.0 if proxy_liquidity else 0.0)
    missing_penalty = 0.0
    for key in ("price_pct_5m", "market_cap_usd", "liquidity_usd"):
        if token.get(key) in {None, ""}:
            missing_penalty += 4.0

    score = (
        rank_component
        + mom_component
        + momentum_txns_bonus
        + liquidity_quality
        + route_component
        + age_component
        - risk_penalty
        - proxy_penalty
        - extreme_penalty
        - missing_penalty
    )
    return GreenScoreBreakdown(
        score=round(max(0.0, score), 3),
        rank_component=round(rank_component, 3),
        momentum_component=round(mom_component + momentum_txns_bonus, 3),
        liquidity_quality_component=round(liquidity_quality, 3),
        route_component=round(route_component, 3),
        age_component=round(age_component, 3),
        risk_penalty=round(risk_penalty, 3),
        proxy_penalty=round(proxy_penalty, 3),
        extreme_momentum_penalty=round(extreme_penalty, 3),
        missing_data_penalty=round(missing_penalty, 3),
    )


__all__ = ["GreenScoreBreakdown", "momentum_component", "score_green_sniper"]
