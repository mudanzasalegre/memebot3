from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analytics.liquidity_risk import evaluate_liquidity_risk
from config.config import CFG
from ml.lane_taxonomy import LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH


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


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class LateMomentumDecision:
    action: str
    lane: str
    reason: str
    score: float
    reject_reasons: tuple[str, ...]
    route_proxy: bool = False


def evaluate_late_momentum_watch(token: dict[str, Any], *, dry_run: bool, live: bool) -> LateMomentumDecision:
    if not bool(getattr(CFG, "LATE_MOMENTUM_WATCH_ENABLED", True)):
        return LateMomentumDecision("reject", LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH, "disabled", 0.0, ("disabled",))

    price5m = _float(token.get("price_pct_5m"), 0.0)
    txns = _float(token.get("txns_last_5m"), 0.0)
    liq = _float(token.get("liquidity_usd"), 0.0)
    mcap = _float(token.get("market_cap_usd"), 0.0)
    impact = _float(token.get("price_impact_pct"), 0.0)
    rank_raw = token.get("rank_score") or token.get("research_rank_score")
    rank = _float(rank_raw, -1.0)
    route = _bool(token.get("has_jupiter_route"))
    proxy = _bool(token.get("liquidity_is_proxy") or token.get("liquidity_usd_is_proxy"))

    min_price = _float(getattr(CFG, "LATE_MOMENTUM_WATCH_MIN_PRICE5M", 300.0), 300.0)
    max_price = _float(getattr(CFG, "LATE_MOMENTUM_WATCH_MAX_PRICE5M", 750.0), 750.0)
    min_rank = _float(getattr(CFG, "LATE_MOMENTUM_WATCH_MIN_RANK_SCORE", 55.0), 55.0)
    min_txns = _float(getattr(CFG, "LATE_MOMENTUM_WATCH_MIN_TXNS_5M", 300), 300.0)
    min_liq = _float(getattr(CFG, "LATE_MOMENTUM_WATCH_MIN_LIQUIDITY_USD", 2000.0), 2000.0)
    max_impact = _float(getattr(CFG, "LATE_MOMENTUM_WATCH_MAX_PRICE_IMPACT_PCT", 12.0), 12.0)
    allow_rank_missing_paper = bool(getattr(CFG, "LATE_MOMENTUM_WATCH_ALLOW_RANK_MISSING_PAPER", True))
    require_route_paper = bool(getattr(CFG, "LATE_MOMENTUM_WATCH_REQUIRE_ROUTE_PAPER", False))
    require_route_live = bool(getattr(CFG, "LATE_MOMENTUM_WATCH_REQUIRE_ROUTE_LIVE", True))
    tag_paper_route_proxy = bool(getattr(CFG, "LATE_MOMENTUM_WATCH_PAPER_ROUTE_PROXY_TAG", True))
    route_proxy = bool(dry_run and not live and not route and not require_route_paper and tag_paper_route_proxy)

    failures: list[str] = []
    if price5m < min_price:
        failures.append("price5m_below_late_watch")
    if price5m > max_price:
        failures.append("price5m_too_extreme")
    if txns < min_txns:
        failures.append("txns_below_min")
    if liq < min_liq:
        failures.append("liq_below_min")
    if proxy and liq < 5000:
        failures.append("proxy_liquidity")
    if mcap <= 0:
        failures.append("missing_mcap")
    elif not (8_000 <= mcap <= 80_000):
        failures.append("mcap_out_of_band")
    if impact > max_impact:
        failures.append("impact_high")
    if live and require_route_live and not route:
        failures.append("no_route_live")
    if dry_run and not live and require_route_paper and not route:
        failures.append("no_route_paper")
    if rank < min_rank and not (dry_run and allow_rank_missing_paper and rank < 0):
        failures.append("rank_below_min")
    liq_decision = evaluate_liquidity_risk(token, live=live)
    if bool(getattr(CFG, "GREEN_SNIPER_LIQ_GUARD_ENABLED", True)) and liq_decision.risk_level in {"high", "lethal"}:
        failures.extend(f"liquidity_risk:{reason}" for reason in liq_decision.reasons)

    score = 0.0
    score += min(max(price5m - min_price, 0.0), 250.0) / 10.0
    score += min(txns, 800.0) / 20.0
    score += min(liq / 500.0, 20.0)
    score += 15.0 if route else 0.0
    score += 15.0 if rank >= min_rank else 0.0
    score -= 15.0 if proxy else 0.0
    score -= max(0.0, impact - 8.0)

    if failures:
        return LateMomentumDecision("shadow", LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH, ",".join(failures[:8]), round(max(score, 0.0), 3), tuple(failures), route_proxy)
    if live and not bool(getattr(CFG, "LATE_MOMENTUM_WATCH_LIVE_ENABLED", False)):
        return LateMomentumDecision("shadow", LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH, "live_disabled", round(score, 3), ("live_disabled",), route_proxy)
    if dry_run and bool(getattr(CFG, "LATE_MOMENTUM_WATCH_PAPER_CANARY_ENABLED", True)):
        return LateMomentumDecision("buy", LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH, "late_momentum_canary", round(score, 3), (), route_proxy)
    return LateMomentumDecision("shadow", LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH, "watch_only", round(score, 3), ("watch_only",), route_proxy)


__all__ = ["LateMomentumDecision", "evaluate_late_momentum_watch"]
