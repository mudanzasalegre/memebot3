from __future__ import annotations

from typing import Any

from runtime.entry_policy import build_trade_decision_v2


def learned_green_sniper_policy(token: dict[str, Any], scores: dict[str, Any], *, base_action: str, live: bool = False):
    return build_trade_decision_v2(
        token=token,
        base_action=base_action,
        amount_sol=float(token.get("amount_sol") or 0.0),
        scores=scores,
        reason="green_sniper_learned_policy_v1",
        policy_version="green_sniper_learned_v1",
        live=live,
    )


def learned_research_rank_policy(token: dict[str, Any], scores: dict[str, Any], *, base_action: str, live: bool = False):
    rank = float(scores.get("rank_score") or token.get("rank_score") or 0.0)
    scores = {**scores, "policy_score": float(scores.get("policy_score") or 0.0) + rank * 0.1}
    return build_trade_decision_v2(
        token=token,
        base_action=base_action,
        amount_sol=float(token.get("amount_sol") or token.get("research_rank_canary_amount_sol") or 0.0),
        scores=scores,
        reason="research_rank_learned_canary_v1",
        policy_version="research_rank_learned_v1",
        live=live,
    )


def learned_late_momentum_policy(token: dict[str, Any], scores: dict[str, Any], *, base_action: str, live: bool = False):
    if float(token.get("price_pct_5m") or 0.0) >= 300 and scores.get("continuation_score") is None:
        scores = {**scores, "policy_score": min(float(scores.get("policy_score") or 0.0), -1.0)}
    return build_trade_decision_v2(
        token=token,
        base_action=base_action,
        amount_sol=float(token.get("amount_sol") or 0.0),
        scores=scores,
        reason="late_momentum_continuation_v1",
        policy_version="late_momentum_learned_v1",
        live=live,
    )


__all__ = ["learned_green_sniper_policy", "learned_late_momentum_policy", "learned_research_rank_policy"]
