from __future__ import annotations

from runtime.candidate_priority import candidate_priority_score


def test_rank_score_boosts_hot_priority() -> None:
    low = candidate_priority_score({"price_pct_5m": 80, "txns_last_5m": 80, "liquidity_usd": 5000, "rank_score": 20}, source="pumpfun")
    high = candidate_priority_score({"price_pct_5m": 80, "txns_last_5m": 80, "liquidity_usd": 5000, "rank_score": 70}, source="pumpfun")
    assert high > low
