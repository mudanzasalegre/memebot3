from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analytics.ml_policy import MlPolicyDecision
from config.config import CFG
from ml.lane_taxonomy import LANE_UNKNOWN, RESEARCH_LANES, normalize_entry_lane


@dataclass(frozen=True)
class TradeDecision:
    address: str
    entry_lane: str
    should_execute: bool
    execution_mode: str
    amount_sol: float
    reasons: list[str]
    ml: MlPolicyDecision


def build_trade_decision(
    *,
    token: dict[str, Any],
    ml: MlPolicyDecision,
    dry_run: bool,
    base_amount_sol: float | None = None,
) -> TradeDecision:
    lane = normalize_entry_lane(ml.lane or token.get("entry_lane"))
    reasons = [ml.reason]
    execution_mode = "paper" if dry_run else "live"
    should_execute = bool(ml.allow_buy)
    if lane in RESEARCH_LANES and not dry_run and not bool(getattr(CFG, "ML_ALLOW_RESEARCH_LIVE", False)):
        should_execute = False
        reasons.append("research_live_disabled")
    if lane == LANE_UNKNOWN and not dry_run and not bool(getattr(CFG, "ML_ALLOW_UNKNOWN_LIVE", False)):
        should_execute = False
        reasons.append("unknown_live_disabled")
    amount = float(base_amount_sol if base_amount_sol is not None else getattr(CFG, "TRADE_AMOUNT_SOL", 0.0) or 0.0)
    amount *= max(0.0, float(ml.sizing_multiplier or 0.0))
    max_amount = getattr(CFG, "MAX_TRADE_AMOUNT_SOL", None)
    if max_amount is not None:
        try:
            amount = min(amount, float(max_amount))
        except Exception:
            pass
    return TradeDecision(
        address=str(token.get("address") or token.get("mint") or ""),
        entry_lane=lane,
        should_execute=bool(should_execute),
        execution_mode=execution_mode,
        amount_sol=float(amount),
        reasons=reasons,
        ml=ml,
    )


__all__ = ["TradeDecision", "build_trade_decision"]
