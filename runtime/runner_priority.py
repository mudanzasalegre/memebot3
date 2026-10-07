"""Bounded learned queue priority, never a buy veto or safety-gate bypass."""
from __future__ import annotations

import math
from typing import Any

from config.config import CFG


def learned_runner_priority(token: dict[str, Any]) -> dict[str, Any]:
    result = {"bonus": 0.0, "rank_percentiles": {}, "mode": "ranking_only", "buy_permission": False}
    if not bool(getattr(CFG, "SNIPER_LEARNING_PRIORITY_ENABLED", True)):
        result["reason"] = "disabled"
        return result
    # Bare birth notifications cannot be treated as fully measured snapshots.
    for key in ("price_pct_5m", "txns_last_5m", "liquidity_usd", "market_cap_usd"):
        try:
            value = float(token[key])
            if not math.isfinite(value) or (key != "price_pct_5m" and value <= 0):
                raise ValueError(key)
        except (KeyError, TypeError, ValueError):
            result["reason"] = "incomplete_market_snapshot"
            return result
    try:
        from analytics.model_runtime_common import predict_ranking_score
        from features.builder import build_feature_vector
        vector = build_feature_vector(token)
        weights = {50: .06, 100: .12, 200: .04, 300: .04, 500: .08,
                   1000: .05, 2000: .04, 5000: .03, 10000: .02}
        for threshold, weight in weights.items():
            rank = predict_ranking_score("runner", f"runner_{threshold}", vector)
            if rank is not None and math.isfinite(rank):
                result["rank_percentiles"][f"runner_{threshold}"] = rank
                result["bonus"] += max(0.0, rank - 50) * weight
        result["bonus"] = round(min(20.0, result["bonus"]), 4)
        result["reason"] = "validated_runner_ranking" if result["rank_percentiles"] else "no_validated_runner_ranker"
    except Exception:
        result["reason"] = "ranking_unavailable"
        result["bonus"] = 0.0
    return result


__all__ = ["learned_runner_priority"]
