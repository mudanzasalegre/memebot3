from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PolicyScoreWeights:
    runner100: float = 25.0
    runner300: float = 75.0
    runner500: float = 125.0
    continuation: float = 1.0
    risk30: float = 30.0
    risk50: float = 60.0
    liquidity_risk: float = 10.0


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


def compute_policy_score(scores: dict[str, Any], *, weights: PolicyScoreWeights | None = None) -> float:
    w = weights or PolicyScoreWeights()
    score = _float(scores.get("ev_pred_pct") or scores.get("ev_score"))
    score += _float(scores.get("runner100_proba")) * w.runner100
    score += _float(scores.get("runner300_proba")) * w.runner300
    score += _float(scores.get("runner500_proba")) * w.runner500
    score += _float(scores.get("continuation_score")) * w.continuation
    score -= _float(scores.get("risk_proba_30")) * w.risk30
    score -= _float(scores.get("risk_proba_50")) * w.risk50
    score -= _float(scores.get("liquidity_risk_penalty")) * w.liquidity_risk
    return round(score, 6)


__all__ = ["PolicyScoreWeights", "compute_policy_score"]
