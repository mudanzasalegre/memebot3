from __future__ import annotations

from analytics.green_sniper_score import score_green_sniper


def test_extreme_price5m_does_not_dominate_score() -> None:
    sweet = score_green_sniper(
        {"price_pct_5m": 80, "txns_last_5m": 150, "liquidity_usd": 6000, "market_cap_usd": 30000, "rank_score": 65},
        has_route=True,
        proxy_liquidity=False,
        live=False,
    ).score
    extreme = score_green_sniper(
        {"price_pct_5m": 600, "txns_last_5m": 150, "liquidity_usd": 6000, "market_cap_usd": 30000, "rank_score": 65},
        has_route=True,
        proxy_liquidity=False,
        live=False,
    ).score
    assert sweet > extreme
