from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.config import CFG


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _bool_cfg(name: str, default: bool) -> bool:
    return bool(getattr(CFG, name, default))


@dataclass(frozen=True)
class GreenSniperRankGuardDecision:
    allowed: bool
    rank_score: float
    min_score: float
    reason: str


def evaluate_green_sniper_rank_guard(rank_info: dict[str, Any] | None) -> GreenSniperRankGuardDecision:
    """Precision guard for green-sniper buys.

    The green candle gate detects hot newborns; this guard is deliberately
    separate so rejected hot candidates can still be shadowed for learning.
    """

    rank_score = _to_float((rank_info or {}).get("rank_score"), 0.0)
    min_score = _to_float(getattr(CFG, "GREEN_SNIPER_RANK_GUARD_MIN_SCORE", 54.0), 54.0)
    if not _bool_cfg("GREEN_SNIPER_RANK_GUARD_ENABLED", True):
        return GreenSniperRankGuardDecision(
            allowed=True,
            rank_score=rank_score,
            min_score=min_score,
            reason="disabled",
        )
    if rank_score >= min_score:
        return GreenSniperRankGuardDecision(
            allowed=True,
            rank_score=rank_score,
            min_score=min_score,
            reason="rank_ok",
        )
    return GreenSniperRankGuardDecision(
        allowed=False,
        rank_score=rank_score,
        min_score=min_score,
        reason=f"rank_score_below_min:{rank_score:.2f}<{min_score:.2f}",
    )


__all__ = ["GreenSniperRankGuardDecision", "evaluate_green_sniper_rank_guard"]
