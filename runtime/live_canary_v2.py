from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.config import CFG


@dataclass(frozen=True)
class LiveCanaryDecision:
    allowed: bool
    reason: str
    max_open: int
    max_daily_buys: int
    size_sol: float


def evaluate_live_canary_v2(
    token: dict[str, Any],
    *,
    candidate_policy_passed: bool,
    paper_forward_passed: bool | None = None,
    manual_approval: bool,
    provider_health_ok: bool,
    open_count: int = 0,
    daily_buys: int = 0,
    daily_loss_sol: float = 0.0,
) -> LiveCanaryDecision:
    max_open = int(getattr(CFG, "LIVE_CANARY_MAX_OPEN", 1) or 1)
    max_daily = int(getattr(CFG, "LIVE_CANARY_MAX_DAILY_BUYS", 3) or 3)
    loss_cap = float(getattr(CFG, "LIVE_CANARY_DAILY_LOSS_CAP_SOL", 0.05) or 0.05)
    size_sol = float(getattr(CFG, "LIVE_CANARY_SIZE_SOL", getattr(CFG, "MIN_BUY_SOL", 0.01)) or 0.01)
    if not bool(getattr(CFG, "LIVE_CANARY_ENABLED", False)):
        return LiveCanaryDecision(False, "live_canary_disabled", max_open, max_daily, size_sol)
    if not candidate_policy_passed:
        return LiveCanaryDecision(False, "candidate_policy_not_passed", max_open, max_daily, size_sol)
    if paper_forward_passed is None:
        paper_forward_passed = candidate_policy_passed
    if not paper_forward_passed:
        return LiveCanaryDecision(False, "paper_forward_not_passed", max_open, max_daily, size_sol)
    if not manual_approval:
        return LiveCanaryDecision(False, "manual_approval_required", max_open, max_daily, size_sol)
    if not provider_health_ok:
        return LiveCanaryDecision(False, "provider_health_bad", max_open, max_daily, size_sol)
    if bool(getattr(CFG, "LIVE_REQUIRE_ROUTE", True)) and not bool(token.get("has_jupiter_route")):
        return LiveCanaryDecision(False, "route_required", max_open, max_daily, size_sol)
    if str(token.get("risk_level") or token.get("green_sniper_risk_level") or "low").lower() in {"high", "lethal"}:
        return LiveCanaryDecision(False, "risk_not_low", max_open, max_daily, size_sol)
    if open_count >= max_open:
        return LiveCanaryDecision(False, "max_open", max_open, max_daily, size_sol)
    if daily_buys >= max_daily:
        return LiveCanaryDecision(False, "max_daily_buys", max_open, max_daily, size_sol)
    if abs(float(daily_loss_sol)) >= loss_cap:
        return LiveCanaryDecision(False, "daily_loss_cap", max_open, max_daily, size_sol)
    return LiveCanaryDecision(True, "ok", max_open, max_daily, size_sol)


__all__ = ["LiveCanaryDecision", "evaluate_live_canary_v2"]
