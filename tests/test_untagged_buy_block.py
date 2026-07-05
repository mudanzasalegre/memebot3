from __future__ import annotations

from types import SimpleNamespace

from analytics.untagged_buy_block import apply_untagged_breakout_context, evaluate_untagged_buy_guard


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        REQUIRE_ENTRY_LANE_FOR_BUY=True,
        ALLOW_UNTAGGED_STANDARD_BUY=False,
        DEX_MATURE_STANDARD_BUY_ENABLED=False,
        PUMPFUN_STANDARD_BUY_ENABLED=False,
        PUMPSWAP_PRIME_STRICT_ENABLED=True,
        SNIPER_RESEARCH_SUBPROFILES_ENABLED=True,
    )


def test_candidate_without_lane_does_not_buy() -> None:
    decision = evaluate_untagged_buy_guard({"entry_regime": "pump_early"}, cfg=_cfg())

    assert decision.allowed is False
    assert decision.reason == "untagged_buy_blocked"


def test_research_rank_canary_can_buy() -> None:
    decision = evaluate_untagged_buy_guard(
        {
            "entry_lane": "pump_early_research_rank_canary",
            "gate_profile": "research_rank_canary",
            "profit_lane_tier": "pump_early_research_rank_canary",
        },
        cfg=_cfg(),
    )

    assert decision.allowed is True


def test_rebound_prime_can_buy() -> None:
    decision = evaluate_untagged_buy_guard(
        {
            "entry_lane": "pump_early_pumpswap_rebound_prime",
            "gate_profile": "pumpswap_rebound_prime",
            "profit_lane_tier": "pump_early_pumpswap_rebound_prime",
        },
        cfg=_cfg(),
    )

    assert decision.allowed is True


def test_sniper_research_micro_fallback_can_buy() -> None:
    decision = evaluate_untagged_buy_guard(
        {
            "entry_lane": "pump_early_sniper_research_micro_fallback",
            "gate_profile": "sniper_research_micro_fallback",
            "profit_lane_tier": "pump_early_sniper_research_micro_fallback",
            "entry_subprofile": "sniper_research_micro_fallback",
        },
        cfg=_cfg(),
    )

    assert decision.allowed is True


def test_paper_exploration_lane_can_buy() -> None:
    decision = evaluate_untagged_buy_guard(
        {
            "entry_lane": "pump_early_paper_exploration_micro",
            "gate_profile": "paper_exploration_quota",
            "profit_lane_tier": "pump_early_paper_exploration_micro",
        },
        cfg=_cfg(),
    )

    assert decision.allowed is True


def test_shadow_followup_lane_can_buy() -> None:
    decision = evaluate_untagged_buy_guard(
        {
            "entry_lane": "pump_early_shadow_followup_micro",
            "gate_profile": "shadow_followup_micro",
            "profit_lane_tier": "pump_early_shadow_followup_micro",
        },
        cfg=_cfg(),
    )

    assert decision.allowed is True


def test_pumpswap_prime_without_strict_goes_shadow() -> None:
    decision = evaluate_untagged_buy_guard(
        {
            "entry_lane": "pump_early_pumpswap_profit",
            "gate_profile": "pumpswap_profit_prime",
            "profit_lane_tier": "pump_early_pumpswap_prime",
            "pumpswap_prime_strict_passed": False,
            "dex_id": "pumpswap",
            "txns_last_5m": 499,
            "liquidity_usd": 20_000,
            "has_jupiter_route": True,
        },
        cfg=_cfg(),
    )

    assert decision.allowed is False
    assert "pumpswap_prime_not_strict" in decision.failures


def test_untagged_real_liquidity_breakout_gets_shadow_followup_lane() -> None:
    row = {
        "entry_regime": "dex_mature",
        "dex_id": "pumpswap",
        "liquidity_usd": 14_892,
        "liquidity_is_proxy": 0,
        "has_jupiter_route": True,
        "txns_last_5m": 351,
        "volume_24h_usd": 15_108,
        "market_cap_usd": 15_094,
        "age_minutes": 38.2,
        "price_impact_pct": 0.0,
        "price_pct_5m": 121.0,
        "rank_score": 68.1,
        "score_total": 55,
    }

    decision = evaluate_untagged_buy_guard(row, cfg=_cfg())
    apply_untagged_breakout_context(row, decision)

    assert decision.allowed is True
    assert decision.reason == "untagged_real_liquidity_breakout"
    assert row["entry_lane"] == "pump_early_shadow_followup_micro"
    assert row["gate_profile"] == "shadow_followup_micro"
