from __future__ import annotations

from analytics.liquidity_risk import evaluate_liquidity_risk


def test_proxy_low_liq_no_route_is_lethal() -> None:
    decision = evaluate_liquidity_risk(
        {"liquidity_usd": 1000, "liquidity_is_proxy": True, "price_pct_5m": 120, "txns_last_5m": 20},
        live=True,
    )
    assert decision.risk_level == "lethal"
    assert not decision.can_buy_live
