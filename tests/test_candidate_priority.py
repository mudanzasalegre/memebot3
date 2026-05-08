from __future__ import annotations

from runtime.candidate_priority import candidate_priority_score, research_rank_priority_fit


def test_rank_score_boosts_hot_priority() -> None:
    low = candidate_priority_score({"price_pct_5m": 80, "txns_last_5m": 80, "liquidity_usd": 5000, "rank_score": 20}, source="pumpfun")
    high = candidate_priority_score({"price_pct_5m": 80, "txns_last_5m": 80, "liquidity_usd": 5000, "rank_score": 70}, source="pumpfun")
    assert high > low


def test_research_rank_edge_fit_gets_priority_bonus() -> None:
    base = {
        "entry_lane": "pump_early_sniper_research",
        "price_pct_5m": 70,
        "txns_last_5m": 350,
        "liquidity_usd": 12000,
        "market_cap_usd": 50_000,
        "rank_score": 70,
        "has_jupiter_route": 1,
        "liquidity_is_proxy": 0,
    }
    assert research_rank_priority_fit(base)
    edge = candidate_priority_score(base, source="pumpfun")
    non_edge = candidate_priority_score({**base, "liquidity_is_proxy": 1}, source="pumpfun")
    assert edge > non_edge


def test_research_rank_edge_fit_accepts_fractional_rank_score() -> None:
    base = {
        "entry_lane": "pump_early_sniper_research",
        "price_pct_5m": 70,
        "txns_last_5m": 350,
        "liquidity_usd": 12000,
        "market_cap_usd": 50_000,
        "rank_score": 0.70,
        "has_jupiter_route": 1,
        "liquidity_is_proxy": 0,
    }
    assert research_rank_priority_fit(base)
