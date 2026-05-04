from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.config import CFG
from analytics.lane_policy_categories import POLICY_RESEARCH_RANK_CANARY
from ml.lane_taxonomy import LANE_RESEARCH_RANK_CANARY, LANE_RESEARCH_SNIPER, normalize_entry_lane


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


def _score_threshold(value: Any, default: float = 0.647) -> float:
    raw = _float(value, default)
    return raw * 100.0 if 0.0 < raw <= 1.0 else raw


def _field_float(token: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = token.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return _float(value, default)
    return default


@dataclass(frozen=True)
class ResearchRankCanaryDecision:
    allowed: bool
    entry_lane: str
    reason: str
    rank_score: float
    min_score: float
    amount_sol: float


def evaluate_research_rank_canary(
    token: dict[str, Any],
    rank_info: dict[str, Any] | None,
    *,
    dry_run: bool,
    live: bool,
) -> ResearchRankCanaryDecision:
    min_score = _score_threshold(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_SCORE", 0.647), 0.647)
    amount = _float(getattr(CFG, "RESEARCH_RANK_CANARY_SIZE_SOL", 0.01), 0.01)
    if dry_run:
        amount = max(amount, _float(getattr(CFG, "MIN_BUY_SOL", amount), amount))
    rank_score = _float(
        (rank_info or {}).get("rank_score")
        or (rank_info or {}).get("research_rank_score")
        or token.get("rank_score")
        or token.get("research_rank_score"),
        0.0,
    )
    if not bool(getattr(CFG, "RESEARCH_RANK_CANARY_ENABLED", True)):
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, "disabled", rank_score, min_score, amount)
    if normalize_entry_lane(token.get("entry_lane")) != LANE_RESEARCH_SNIPER:
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, "not_research_sniper", rank_score, min_score, amount)
    if live and not bool(getattr(CFG, "RESEARCH_RANK_CANARY_LIVE_ENABLED", False)):
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, "live_disabled", rank_score, min_score, amount)
    if dry_run and not bool(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_ENABLED", True)):
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, "paper_disabled", rank_score, min_score, amount)
    if rank_score < min_score:
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, f"rank_below_min:{rank_score:.2f}<{min_score:.2f}", rank_score, min_score, amount)
    price5m = _field_float(token, "price_pct_5m", "buy_price_pct_5m", default=0.0)
    min_price5m = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_PRICE5M", 25.0), 25.0)
    max_price5m = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_PRICE5M", 100.0), 100.0)
    if price5m < min_price5m or price5m > max_price5m:
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, f"price5m_out_of_band:{price5m:.2f}", rank_score, min_score, amount)
    mcap = _field_float(token, "market_cap_usd", "buy_market_cap_usd", default=0.0)
    min_mcap = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_MCAP_USD", 25_000.0), 25_000.0)
    max_mcap = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_MCAP_USD", 100_000.0), 100_000.0)
    if mcap < min_mcap or mcap > max_mcap:
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, f"mcap_out_of_band:{mcap:.0f}", rank_score, min_score, amount)
    txns = _field_float(token, "txns_last_5m", "buy_txns_last_5m", default=0.0)
    min_txns = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_TXNS_5M", 300), 300.0)
    if txns < min_txns:
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, f"txns_below_min:{txns:.0f}<{min_txns:.0f}", rank_score, min_score, amount)
    liq = _field_float(token, "liquidity_usd", "buy_liquidity_usd", default=0.0)
    min_liq = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_LIQUIDITY_USD", 2000.0), 2000.0)
    if liq < min_liq:
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, f"liquidity_below_min:{liq:.0f}<{min_liq:.0f}", rank_score, min_score, amount)
    proxy = _bool(token.get("liquidity_is_proxy") or token.get("liquidity_usd_is_proxy") or token.get("buy_liquidity_is_proxy"))
    if proxy and bool(getattr(CFG, "RESEARCH_RANK_CANARY_PREFER_REAL_LIQUIDITY", True)):
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, "proxy_liquidity", rank_score, min_score, amount)
    if dry_run and bool(getattr(CFG, "RESEARCH_RANK_CANARY_REQUIRE_ROUTE_PAPER", True)) and not _bool(token.get("has_jupiter_route")):
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, "no_route_paper", rank_score, min_score, amount)
    if live and bool(getattr(CFG, "RESEARCH_RANK_CANARY_REQUIRE_ROUTE_LIVE", True)) and not _bool(token.get("has_jupiter_route")):
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, "no_route_live", rank_score, min_score, amount)
    return ResearchRankCanaryDecision(True, LANE_RESEARCH_RANK_CANARY, "research_rank_canary", rank_score, min_score, amount)


def apply_research_rank_canary_context(token: dict[str, Any], decision: ResearchRankCanaryDecision) -> dict[str, Any]:
    token["entry_lane"] = decision.entry_lane
    token["gate_profile"] = "research_rank_canary"
    token["profit_lane_tier"] = decision.entry_lane
    token["lane_policy_category"] = POLICY_RESEARCH_RANK_CANARY
    token["research_rank_canary_rank_score"] = decision.rank_score
    token["research_rank_canary_min_score"] = decision.min_score
    token["research_rank_canary_amount_sol"] = decision.amount_sol
    token["green_sniper_reason"] = decision.reason
    token["live_profit_gate_failed_count"] = 0
    return token


__all__ = ["ResearchRankCanaryDecision", "apply_research_rank_canary_context", "evaluate_research_rank_canary"]
