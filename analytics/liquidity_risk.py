from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.config import CFG


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LiquidityRiskDecision:
    risk_level: str
    can_buy_paper: bool
    can_buy_live: bool
    reasons: tuple[str, ...]


def evaluate_liquidity_risk(token: dict[str, Any], *, live: bool) -> LiquidityRiskDecision:
    if not bool(getattr(CFG, "GREEN_SNIPER_LIQ_GUARD_ENABLED", True)):
        return LiquidityRiskDecision("low", True, True, ("disabled",))
    liq = _float(token.get("liquidity_usd"), 0.0)
    price5m = _float(token.get("price_pct_5m"), 0.0)
    txns = _float(token.get("txns_last_5m"), 0.0)
    impact = _float(token.get("price_impact_pct"), 0.0)
    proxy = _bool(token.get("liquidity_is_proxy") or token.get("liquidity_usd_is_proxy"))
    route = _bool(token.get("has_jupiter_route"))
    reasons: list[str] = []

    if proxy and price5m >= float(getattr(CFG, "GREEN_SNIPER_LIQ_PROXY_MAX_PRICE5M", 100.0)) and txns < float(getattr(CFG, "GREEN_SNIPER_LIQ_PROXY_MIN_TXNS_FOR_EXCEPTION", 300)):
        reasons.append("proxy_liquidity_high_momentum")
    if liq <= 1200 and proxy and not route:
        reasons.append("proxy_low_liq_no_route")
    if not proxy and liq < float(getattr(CFG, "GREEN_SNIPER_REAL_LIQ_MIN_FOR_HOT", 2500.0)) and impact > 12:
        reasons.append("low_real_liq_high_impact")

    if "proxy_low_liq_no_route" in reasons:
        return LiquidityRiskDecision("lethal", False, False, tuple(reasons))
    if reasons:
        return LiquidityRiskDecision("high", bool(getattr(CFG, "GREEN_SNIPER_LIQ_CRUSH_SHADOW_IN_PAPER", True)) is False, False, tuple(reasons))
    return LiquidityRiskDecision("low", True, True, ("ok",))


__all__ = ["LiquidityRiskDecision", "evaluate_liquidity_risk"]
