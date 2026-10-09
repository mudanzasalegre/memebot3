"""Bounded learned queue priority, never a buy veto or safety-gate bypass."""
from __future__ import annotations

import math
from hashlib import sha256
import json
from copy import deepcopy
from typing import Any

from config.config import CFG


RUNNER_PRIORITY_WEIGHTS = {50: .06, 100: .12, 200: .04, 300: .04, 500: .08,
                           1000: .05, 2000: .04, 5000: .03, 10000: .02}


def runner_priority_generation() -> str:
    """Checked selector/content identity; timestamps cannot certify a ranker.

    The caller owns the inference scope. Unavailable/disabled generations have
    their own identity, so a pending old bonus cannot survive invalidation.
    """
    enabled = bool(getattr(CFG, "SNIPER_LEARNING_PRIORITY_ENABLED", True))
    selection = None
    if enabled:
        try:
            from analytics.model_runtime_common import family_model_selection
            selection = family_model_selection("runner", targets=[f"runner_{t}" for t in RUNNER_PRIORITY_WEIGHTS])
        except Exception:
            selection = {"status": "unavailable"}
    return sha256(json.dumps({"enabled": enabled, "selection": selection}, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def learned_runner_priority(token: dict[str, Any]) -> dict[str, Any]:
    result = {"bonus": 0.0, "rank_percentiles": {}, "mode": "ranking_only", "buy_permission": False}
    if not bool(getattr(CFG, "SNIPER_LEARNING_PRIORITY_ENABLED", True)):
        result["reason"] = "disabled"
        return result
    # Bare birth notifications cannot be treated as fully measured snapshots.
    for key in ("price_pct_5m", "txns_last_5m", "liquidity_usd", "market_cap_usd"):
        try:
            if isinstance(token[key], bool):
                raise ValueError(key)
            value = float(token[key])
            if not math.isfinite(value) or (key != "price_pct_5m" and value <= 0):
                raise ValueError(key)
        except (KeyError, TypeError, ValueError):
            result["reason"] = "incomplete_market_snapshot"
            return result
    try:
        from analytics.inference_scope import ensure_inference_scope
        from analytics.model_runtime_common import predict_ranking_score, family_model_selection
        from features.builder import build_feature_vector
        vector = build_feature_vector(token)
        weights = RUNNER_PRIORITY_WEIGHTS
        with ensure_inference_scope():
            for threshold, weight in weights.items():
                rank = predict_ranking_score("runner", f"runner_{threshold}", vector)
                if rank is not None and math.isfinite(rank):
                    result["rank_percentiles"][f"runner_{threshold}"] = rank
                    result["bonus"] += max(0.0, rank - 50) * weight
            result["model_selection"] = family_model_selection("runner", targets=[f"runner_{t}" for t in weights])
        result["bonus"] = round(min(20.0, result["bonus"]), 4)
        result["reason"] = "validated_runner_ranking" if result["rank_percentiles"] else "no_validated_runner_ranker"
    except Exception:
        result["reason"] = "ranking_unavailable"
        result["bonus"] = 0.0
        result["rank_percentiles"] = {}
    return result


def learned_runner_priorities(tokens: list[dict[str, Any]], *, now=None) -> list[dict[str, Any]]:
    """Batch queue repricing, with independent missing/unproved rows neutral."""
    if not isinstance(tokens, list) or len(tokens) > 1000:
        raise ValueError("Priority batch must contain at most 1000 snapshots")
    results, positions, vectors = [], [], []
    from features.builder import build_feature_vector
    from analytics.inference_scope import ensure_inference_scope
    from analytics.model_runtime_common import predict_ranking_scores, family_model_selection
    enabled = bool(getattr(CFG, "SNIPER_LEARNING_PRIORITY_ENABLED", True))
    for index, token in enumerate(tokens):
        result = {"bonus": 0., "rank_percentiles": {}, "mode": "ranking_only", "buy_permission": False,
                  "reason": "disabled" if not enabled else "incomplete_market_snapshot"}
        results.append(result)
        if not enabled:
            continue
        try:
            for key in ("price_pct_5m", "txns_last_5m", "liquidity_usd", "market_cap_usd"):
                value = float(token[key])
                if isinstance(token[key], bool) or not math.isfinite(value) or key != "price_pct_5m" and value <= 0:
                    raise ValueError(key)
            result["reason"] = "ranking_unavailable"
            vectors.append(build_feature_vector(token, now=now))
            positions.append(index)
        except (KeyError, ValueError, TypeError, AssertionError, OverflowError):
            continue
    if vectors:
        try:
            with ensure_inference_scope():
                selection = family_model_selection("runner", targets=[f"runner_{t}" for t in RUNNER_PRIORITY_WEIGHTS])
                for threshold, weight in RUNNER_PRIORITY_WEIGHTS.items():
                    target = f"runner_{threshold}"
                    ranks = predict_ranking_scores("runner", target, vectors)
                    for position, rank in zip(positions, ranks):
                        if rank is not None and math.isfinite(rank):
                            result = results[position]
                            result["rank_percentiles"][target] = rank
                            result["bonus"] += max(0., rank - 50) * weight
                for position in positions:
                    result = results[position]
                    result["model_selection"] = deepcopy(selection)
                    result["bonus"] = round(min(20., result["bonus"]), 4)
                    result["reason"] = "validated_runner_ranking" if result["rank_percentiles"] else "no_validated_runner_ranker"
        except Exception:
            for position in positions:
                results[position].update(bonus=0., rank_percentiles={}, reason="ranking_unavailable")
    return results


__all__ = ["learned_runner_priority", "learned_runner_priorities", "runner_priority_generation"]
