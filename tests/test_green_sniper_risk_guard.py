from __future__ import annotations

from analytics.green_sniper_risk_guard import evaluate_green_sniper_risk_guard


def test_high_risk_proxy_momentum_goes_shadow_in_paper() -> None:
    token = {
        "liquidity_usd": 1200,
        "liquidity_is_proxy": True,
        "price_pct_5m": 180,
        "txns_last_5m": 80,
        "market_cap_usd": 20_000,
    }
    decision = evaluate_green_sniper_risk_guard(token, dry_run=True, live=False)
    assert not decision.allow_buy
    assert decision.can_shadow
    assert decision.risk_level in {"high", "lethal"}


def test_rank_liquidity_route_passes_low_risk() -> None:
    token = {
        "liquidity_usd": 8000,
        "liquidity_is_proxy": False,
        "price_pct_5m": 80,
        "txns_last_5m": 180,
        "market_cap_usd": 40_000,
        "rank_score": 70,
        "has_jupiter_route": True,
        "price_impact_pct": 5,
    }
    decision = evaluate_green_sniper_risk_guard(token, dry_run=False, live=True)
    assert decision.allow_buy
    assert decision.risk_level == "low"
