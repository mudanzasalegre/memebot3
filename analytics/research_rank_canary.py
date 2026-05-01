from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.config import CFG
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
    min_score = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_SCORE", 61.15), 61.15)
    amount = _float(getattr(CFG, "RESEARCH_RANK_CANARY_SIZE_SOL", 0.01), 0.01)
    if dry_run:
        amount = max(amount, _float(getattr(CFG, "MIN_BUY_SOL", amount), amount))
    rank_score = _float((rank_info or {}).get("rank_score") or token.get("rank_score"), 0.0)
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
    liq = _float(token.get("liquidity_usd"), 0.0)
    min_liq = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_LIQUIDITY_USD", 2000.0), 2000.0)
    if liq < min_liq:
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, f"liquidity_below_min:{liq:.0f}<{min_liq:.0f}", rank_score, min_score, amount)
    if live and bool(getattr(CFG, "RESEARCH_RANK_CANARY_REQUIRE_ROUTE_LIVE", True)) and not _bool(token.get("has_jupiter_route")):
        return ResearchRankCanaryDecision(False, LANE_RESEARCH_RANK_CANARY, "no_route_live", rank_score, min_score, amount)
    return ResearchRankCanaryDecision(True, LANE_RESEARCH_RANK_CANARY, "research_rank_canary", rank_score, min_score, amount)


def apply_research_rank_canary_context(token: dict[str, Any], decision: ResearchRankCanaryDecision) -> dict[str, Any]:
    token["entry_lane"] = decision.entry_lane
    token["gate_profile"] = "research_rank_canary"
    token["profit_lane_tier"] = decision.entry_lane
    token["research_rank_canary_rank_score"] = decision.rank_score
    token["research_rank_canary_min_score"] = decision.min_score
    token["research_rank_canary_amount_sol"] = decision.amount_sol
    token["green_sniper_reason"] = decision.reason
    token["live_profit_gate_failed_count"] = 0
    return token


__all__ = ["ResearchRankCanaryDecision", "apply_research_rank_canary_context", "evaluate_research_rank_canary"]
