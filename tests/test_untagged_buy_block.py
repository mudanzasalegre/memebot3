from __future__ import annotations

from types import SimpleNamespace

from analytics.untagged_buy_block import evaluate_untagged_buy_guard


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
