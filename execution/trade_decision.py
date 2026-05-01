from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class TradeDecisionScores:
    green_score: float | None = None
    rank_score: float | None = None
    risk_proba_30: float | None = None
    risk_proba_50: float | None = None
    ev_pred_pct: float | None = None
    runner100_proba: float | None = None
    runner300_proba: float | None = None
    runner500_proba: float | None = None
    continuation_score: float | None = None
    policy_score: float | None = None


@dataclass(frozen=True)
class TradeDecision:
    address: str
    lane: str
    action: str
    amount_sol: float
    exit_profile: str
    reason: str
    scores: TradeDecisionScores = field(default_factory=TradeDecisionScores)
    policy_version: str = "legacy"
    config_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_action(action: str) -> str:
    raw = str(action or "").strip().lower()
    if raw in {"buy", "shadow", "reject", "delay"}:
        return raw
    if raw in {"bought", "paper_buy", "buy_ok"}:
        return "buy"
    if raw in {"wait"}:
        return "delay"
    return "reject"


__all__ = ["TradeDecision", "TradeDecisionScores", "normalize_action"]
