from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analytics.liquidity_risk import evaluate_liquidity_risk
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


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")


@dataclass(frozen=True)
class RiskGuardDecision:
    allow_buy: bool
    can_shadow: bool
    risk_level: str
    risk_reasons: tuple[str, ...]
    size_multiplier: float


def evaluate_green_sniper_risk_guard(token: dict[str, Any], *, dry_run: bool, live: bool) -> RiskGuardDecision:
    if not bool(getattr(CFG, "GREEN_SNIPER_RISK_GUARD_ENABLED", True)):
        return RiskGuardDecision(True, True, "low", ("disabled",), 1.0)
    price5m = _float(token.get("price_pct_5m"), 0.0)
    liq = _float(token.get("liquidity_usd"), 0.0)
    mcap = _float(token.get("market_cap_usd"), 0.0)
    txns = _float(token.get("txns_last_5m"), 0.0)
    impact = _float(token.get("price_impact_pct"), 0.0)
    rank = _float(token.get("rank_score") or token.get("research_rank_score"), 0.0)
    proxy = _bool(token.get("liquidity_is_proxy") or token.get("liquidity_usd_is_proxy"))
    route = _bool(token.get("has_jupiter_route"))
    dex_id = _norm(token.get("dex_id") or token.get("dexId"))
    source = _norm(token.get("discovered_via") or token.get("source"))

    reasons: list[str] = []
    liq_decision = evaluate_liquidity_risk(token, live=live)
    if liq_decision.risk_level in {"high", "lethal"}:
        reasons.extend(liq_decision.reasons)
    if proxy and bool(getattr(CFG, "GREEN_SNIPER_BLOCK_PROXY_PRODUCTIVE", True)):
        reasons.append("proxy_liquidity_productive_block")
    if proxy and price5m >= 100 and txns < 300:
        reasons.append("proxy_liquidity_high_momentum_low_txns")
    if price5m >= 180 and proxy:
        reasons.append("extreme_momentum_proxy_liq")
    if source in {"pumpfun", "pumpportal"} or dex_id == "pumpfun":
        min_real_liq_high_mom = float(
            getattr(CFG, "GREEN_SNIPER_PUMPFUN_HIGH_MOMENTUM_MIN_REAL_LIQUIDITY_USD", 2500.0) or 2500.0
        )
        high_mom_floor = float(getattr(CFG, "GREEN_SNIPER_PUMPFUN_LOW_LIQ_HIGH_MOMENTUM_PCT", 90.0) or 90.0)
        if not proxy and price5m >= high_mom_floor and liq < min_real_liq_high_mom:
            reasons.append("pumpfun_low_real_liq_high_momentum")
    if price5m >= 180 and 0 < mcap < 10_000:
        reasons.append("extreme_momentum_micro_mcap")
    if dex_id == "pumpswap" and 50_000 <= mcap < 100_000:
        reasons.append("pumpswap_mcap_50k_100k_bucket")
    if mcap <= 0 and token.get("price_usd") in {None, ""}:
        reasons.append("missing_price_and_mcap")
    if liq <= 1200 and proxy and _float(token.get("score_total"), 0.0) < 30:
        reasons.append("low_proxy_liq_low_score")
    if impact > (12.0 if live else 20.0):
        reasons.append("high_impact")
    if live and not route:
        reasons.append("no_route_live")

    if not reasons:
        return RiskGuardDecision(True, True, "low", ("ok",), 1.0)

    lethal_markers = {"missing_price_and_mcap", "low_proxy_liq_low_score", "no_route_live"}
    if any(reason in lethal_markers for reason in reasons):
        return RiskGuardDecision(False, True, "lethal", tuple(reasons), 0.0)

    non_bypassable = {
        "proxy_liquidity_productive_block",
        "pumpfun_low_real_liq_high_momentum",
        "pumpswap_mcap_50k_100k_bucket",
    }
    if not any(reason in non_bypassable for reason in reasons) and rank >= 61 and not proxy and route and 50 <= price5m <= 100 and txns >= 100 and 10_000 <= mcap <= 80_000:
        return RiskGuardDecision(True, True, "medium", tuple(reasons), 0.5)

    if dry_run and not live:
        return RiskGuardDecision(False, True, "high", tuple(reasons), 0.0)
    return RiskGuardDecision(False, True, "high", tuple(reasons), 0.0)


__all__ = ["RiskGuardDecision", "evaluate_green_sniper_risk_guard"]
