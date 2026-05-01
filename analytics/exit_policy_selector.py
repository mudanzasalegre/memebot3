from __future__ import annotations

from typing import Any


def select_exit_profile(*, lane: str, scores: dict[str, Any], live: bool = False) -> str:
    risk = float(scores.get("risk_proba_30") or 0.0)
    runner300 = float(scores.get("runner300_proba") or 0.0)
    runner100 = float(scores.get("runner100_proba") or 0.0)
    if live and risk >= 0.35:
        return "defensive"
    if runner300 >= 0.20:
        return "bird_runner"
    if runner100 >= 0.35:
        return "runner"
    if risk >= 0.50:
        return "defensive"
    if str(lane).endswith("late_momentum_watch") and float(scores.get("continuation_score") or 0.0) > 30:
        return "bird_runner"
    return "balanced"


__all__ = ["select_exit_profile"]
