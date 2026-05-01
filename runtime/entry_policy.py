from __future__ import annotations

from typing import Any

from execution.trade_decision import TradeDecision, TradeDecisionScores, normalize_action
from runtime.policy_modes import action_for_mode, mode_for_lane
from runtime.policy_score import compute_policy_score


def build_trade_decision_v2(
    *,
    token: dict[str, Any],
    base_action: str,
    amount_sol: float = 0.0,
    scores: dict[str, Any] | None = None,
    reason: str = "",
    policy_version: str = "policy_v2_observe",
    live: bool = False,
) -> TradeDecision:
    scores = dict(scores or {})
    if "policy_score" not in scores:
        scores["policy_score"] = compute_policy_score(scores)
    lane = str(token.get("entry_lane") or scores.get("lane") or "unknown")
    mode = mode_for_lane(lane, live=live)
    threshold = float(scores.get("policy_score_min") or 0.0)
    risk30 = scores.get("risk_proba_30")
    policy_action = "buy" if float(scores.get("policy_score") or 0.0) >= threshold else "shadow"
    if risk30 is not None and float(risk30) >= float(scores.get("risk_max") or 0.85):
        policy_action = "reject"
    action = action_for_mode(base_action=normalize_action(base_action), policy_action=policy_action, mode=mode)
    score_obj = TradeDecisionScores(
        green_score=scores.get("green_score"),
        rank_score=scores.get("rank_score"),
        risk_proba_30=scores.get("risk_proba_30"),
        risk_proba_50=scores.get("risk_proba_50"),
        ev_pred_pct=scores.get("ev_pred_pct"),
        runner100_proba=scores.get("runner100_proba"),
        runner300_proba=scores.get("runner300_proba"),
        runner500_proba=scores.get("runner500_proba"),
        continuation_score=scores.get("continuation_score"),
        policy_score=scores.get("policy_score"),
    )
    return TradeDecision(
        address=str(token.get("address") or token.get("mint") or ""),
        lane=lane,
        action=action,
        amount_sol=float(amount_sol or 0.0),
        exit_profile=str(token.get("exit_profile") or token.get("runner_exit_profile") or "balanced"),
        reason=reason or f"mode:{mode}",
        scores=score_obj,
        policy_version=policy_version,
        config_hash=str(token.get("config_hash") or ""),
    )


__all__ = ["build_trade_decision_v2"]
