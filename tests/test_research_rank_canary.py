from __future__ import annotations

from analytics.research_rank_canary import (
    apply_research_rank_canary_context,
    apply_research_rank_canary_shadow_context,
    evaluate_research_rank_canary,
)


def test_research_rank_canary_allows_rank_high_paper() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 3000,
        "market_cap_usd": 50_000,
        "price_pct_5m": 70,
        "txns_last_5m": 350,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }
    decision = evaluate_research_rank_canary(token, {"rank_score": 70}, dry_run=True, live=False)
    assert decision.allowed
    assert decision.entry_lane == "pump_early_research_rank_canary"


def test_research_rank_canary_normalizes_fractional_rank_score() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 3000,
        "market_cap_usd": 50_000,
        "price_pct_5m": 70,
        "txns_last_5m": 350,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }
    decision = evaluate_research_rank_canary(token, {"rank_score": 0.70}, dry_run=True, live=False)
    assert decision.allowed
    assert decision.rank_score == 70.0
    assert decision.rank_score_scale == "0_1"


def test_research_rank_canary_uses_exact_reject_reason() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 3000,
        "market_cap_usd": 50_000,
        "price_pct_5m": 70,
        "txns_last_5m": 350,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }
    decision = evaluate_research_rank_canary(token, {"rank_score": 0.10}, dry_run=True, live=False)
    assert not decision.allowed
    assert decision.reason == "rank_below_min"


def test_research_rank_canary_rejects_proxy_liquidity() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 3000,
        "market_cap_usd": 50_000,
        "price_pct_5m": 70,
        "txns_last_5m": 350,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 1,
    }
    decision = evaluate_research_rank_canary(token, {"rank_score": 70}, dry_run=True, live=False)
    assert not decision.allowed
    assert decision.reason == "proxy_liquidity"


def test_research_rank_canary_live_disabled_by_default() -> None:
    token = {"entry_lane": "pump_early_sniper_research", "liquidity_usd": 3000, "has_jupiter_route": True}
    decision = evaluate_research_rank_canary(token, {"rank_score": 70}, dry_run=False, live=True)
    assert not decision.allowed
    assert decision.reason == "live_disabled"


def test_research_rank_canary_allowed_forces_own_lane_over_pumpswap_labels() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "gate_profile": "pumpswap_profit_prime",
        "profit_lane_tier": "pump_early_pumpswap_prime",
        "liquidity_usd": 3000,
        "market_cap_usd": 50_000,
        "price_pct_5m": 70,
        "txns_last_5m": 350,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }
    decision = evaluate_research_rank_canary(token, {"rank_score": 70}, dry_run=True, live=False)

    apply_research_rank_canary_context(token, decision)

    assert decision.allowed
    assert token["entry_lane"] == "pump_early_research_rank_canary"
    assert token["gate_profile"] == "research_rank_canary"
    assert token["profit_lane_tier"] == "pump_early_research_rank_canary"
    assert token["lane_policy_category"] == "research_rank_canary"


def test_research_rank_canary_no_route_shadows_as_own_lane() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 3000,
        "market_cap_usd": 50_000,
        "price_pct_5m": 70,
        "txns_last_5m": 350,
        "has_jupiter_route": False,
        "liquidity_is_proxy": 0,
    }
    decision = evaluate_research_rank_canary(token, {"rank_score": 70}, dry_run=True, live=False)

    apply_research_rank_canary_shadow_context(token, decision)

    assert not decision.allowed
    assert decision.shadow_as_own_lane is True
    assert decision.reason == "research_rank_canary_not_executable:no_route_paper"
    assert token["entry_lane"] == "pump_early_research_rank_canary"
    assert token["research_rank_canary_shadow"] == 1
