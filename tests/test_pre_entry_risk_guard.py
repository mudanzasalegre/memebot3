from __future__ import annotations

from analytics.risk_guards import ACTION_BLOCK, ACTION_DOWNSIZE, ACTION_SHADOW, evaluate_pre_entry_risk


def test_known_liquidity_crush_no_pump_fixture_is_vetoed() -> None:
    decision = evaluate_pre_entry_risk(
        {
            "entry_lane": "pump_early_paper_bootstrap_micro",
            "buy_price_pct_5m": 5.61,
            "buy_liquidity_usd": 53_254.2,
            "buy_market_cap_usd": 465_914.0,
            "buy_txns_last_5m": 866,
            "has_jupiter_route": True,
            "liquidity_is_proxy": False,
            "price_impact_pct": 0.0,
        },
        amount_sol=0.1,
        dry_run=True,
        live=False,
    )

    assert decision.allowed is False
    assert decision.action == ACTION_BLOCK
    assert "high_mcap_no_pump" in decision.failures


def test_proxy_no_route_normal_size_is_forced_to_shadow_in_paper() -> None:
    decision = evaluate_pre_entry_risk(
        {
            "has_jupiter_route": False,
            "liquidity_is_proxy": True,
            "price_impact_pct": 4.0,
            "price_pct_5m": 22.0,
            "liquidity_usd": 20_000,
            "market_cap_usd": 40_000,
            "txns_last_5m": 400,
        },
        amount_sol=0.1,
        dry_run=True,
        live=False,
    )

    assert decision.allowed is False
    assert decision.action == ACTION_SHADOW
    assert decision.force_shadow is True
    assert "no_route" in decision.failures
    assert "proxy_liquidity" in decision.failures


def test_high_price_impact_normal_size_is_forced_to_shadow() -> None:
    decision = evaluate_pre_entry_risk(
        {
            "has_jupiter_route": True,
            "liquidity_is_proxy": False,
            "price_impact_pct": 21.0,
            "price_pct_5m": 35.0,
            "liquidity_usd": 24_000,
            "market_cap_usd": 70_000,
            "txns_last_5m": 500,
        },
        amount_sol=0.1,
        dry_run=True,
        live=False,
    )

    assert decision.allowed is False
    assert decision.action == ACTION_SHADOW
    assert "high_price_impact" in decision.failures


def test_negative_price5m_blocks_without_exception() -> None:
    decision = evaluate_pre_entry_risk(
        {
            "has_jupiter_route": True,
            "liquidity_is_proxy": False,
            "price_pct_5m": -19.74,
            "liquidity_usd": 25_006.62,
            "market_cap_usd": 98_628.0,
            "txns_last_5m": 1179,
            "price_impact_pct": 2.0,
        },
        amount_sol=0.1,
        dry_run=True,
        live=False,
    )

    assert decision.allowed is False
    assert decision.action == ACTION_BLOCK
    assert "price5m_negative" in decision.failures


def test_mild_negative_price5m_can_pass_real_liquidity_exception() -> None:
    decision = evaluate_pre_entry_risk(
        {
            "has_jupiter_route": True,
            "liquidity_is_proxy": False,
            "price_pct_5m": -4.11,
            "liquidity_usd": 12_068.69,
            "market_cap_usd": 25_336.0,
            "txns_last_5m": 349,
            "price_impact_pct": 3.0,
        },
        amount_sol=0.1,
        dry_run=True,
        live=False,
    )

    assert decision.allowed is True
    assert decision.action == "buy"
    assert "price5m_negative_exception" in decision.risk_flags


def test_low_liquidity_normal_size_is_shadowed_under_exact_paper_size() -> None:
    decision = evaluate_pre_entry_risk(
        {
            "has_jupiter_route": True,
            "liquidity_is_proxy": False,
            "price_pct_5m": 40.0,
            "liquidity_usd": 1_200.0,
            "market_cap_usd": 8_500.0,
            "txns_last_5m": 40,
            "price_impact_pct": 5.0,
        },
        amount_sol=0.1,
        dry_run=True,
        live=False,
    )

    assert decision.allowed is False
    assert decision.action == ACTION_SHADOW
    assert decision.amount_sol == 0.0
    assert decision.original_amount_sol == 0.1
    assert {"low_real_liquidity", "low_mcap", "low_txns_5m"}.issubset(set(decision.risk_flags))
