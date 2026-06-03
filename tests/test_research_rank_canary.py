from __future__ import annotations

from analytics.research_rank_canary import (
    apply_research_rank_canary_context,
    apply_research_rank_canary_shadow_context,
    evaluate_research_rank_canary,
    write_research_rank_priority_report,
)


def test_research_rank_canary_allows_rank_high_paper() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 22_000,
        "market_cap_usd": 77_000,
        "price_pct_5m": 70,
        "txns_last_5m": 1200,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }
    decision = evaluate_research_rank_canary(token, {"rank_score": 72}, dry_run=True, live=False)
    assert decision.allowed
    assert decision.reason == "research_rank_canary_priority"
    assert decision.entry_lane == "pump_early_research_rank_canary"


def test_research_rank_canary_normalizes_fractional_rank_score() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 22_000,
        "market_cap_usd": 77_000,
        "price_pct_5m": 70,
        "txns_last_5m": 1200,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }
    decision = evaluate_research_rank_canary(token, {"rank_score": 0.72}, dry_run=True, live=False)
    assert decision.allowed
    assert decision.rank_score == 72.0
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
    assert decision.reason == "shadow_rank_canary"


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
        "liquidity_usd": 22_000,
        "market_cap_usd": 77_000,
        "price_pct_5m": 70,
        "txns_last_5m": 1200,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }
    decision = evaluate_research_rank_canary(token, {"rank_score": 72}, dry_run=True, live=False)

    apply_research_rank_canary_context(token, decision)

    assert decision.allowed
    assert token["entry_lane"] == "pump_early_research_rank_canary"
    assert token["gate_profile"] == "research_rank_canary"
    assert token["profit_lane_tier"] == "pump_early_research_rank_canary"
    assert token["lane_policy_category"] == "research_rank_canary"


def test_research_rank_canary_no_route_shadows_as_own_lane() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 22_000,
        "market_cap_usd": 77_000,
        "price_pct_5m": 70,
        "txns_last_5m": 1200,
        "has_jupiter_route": False,
        "liquidity_is_proxy": 0,
    }
    decision = evaluate_research_rank_canary(token, {"rank_score": 72}, dry_run=True, live=False)

    apply_research_rank_canary_shadow_context(token, decision)

    assert not decision.allowed
    assert decision.shadow_as_own_lane is True
    assert decision.reason == "shadow_rank_canary"
    assert token["entry_lane"] == "pump_early_research_rank_canary"
    assert token["research_rank_canary_shadow"] == 1


def test_research_rank_canary_price5m_below_40_shadows_rank_canary() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 3000,
        "market_cap_usd": 50_000,
        "price_pct_5m": 35,
        "txns_last_5m": 350,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }
    decision = evaluate_research_rank_canary(token, {"rank_score": 70}, dry_run=True, live=False)

    apply_research_rank_canary_shadow_context(token, decision)

    assert not decision.allowed
    assert decision.shadow_as_own_lane is True
    assert decision.reason == "shadow_rank_canary"
    assert token["entry_lane"] == "pump_early_research_rank_canary"
    assert token["research_rank_canary_shadow"] == 1


def test_research_rank_canary_price5m_40_50_low_band_remains_shadow_only() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 3000,
        "market_cap_usd": 50_000,
        "price_pct_5m": 45,
        "txns_last_5m": 350,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }

    blocked = evaluate_research_rank_canary(token, {"rank_score": 66}, dry_run=True, live=False)
    assert not blocked.allowed
    assert blocked.shadow_as_own_lane is True
    assert blocked.reason == "shadow_rank_canary"

    allowed_by_rank = evaluate_research_rank_canary(token, {"rank_score": 70}, dry_run=True, live=False)
    assert not allowed_by_rank.allowed
    assert allowed_by_rank.reason == "shadow_rank_canary"

    token["liquidity_usd"] = 20_000
    allowed_by_liq = evaluate_research_rank_canary(token, {"rank_score": 66}, dry_run=True, live=False)
    assert not allowed_by_liq.allowed
    assert allowed_by_liq.reason == "shadow_rank_canary"


def test_research_rank_canary_elite_consolidation_is_shadow_in_priority_only_mode() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 22_000,
        "market_cap_usd": 50_000,
        "price_pct_5m": 10,
        "txns_last_5m": 350,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }

    decision = evaluate_research_rank_canary(token, {"rank_score": 75}, dry_run=True, live=False)

    assert not decision.allowed
    assert decision.shadow_as_own_lane is True
    assert decision.reason == "shadow_rank_canary"


def test_research_rank_canary_priority_allows_high_quality_50_120_band() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 22_000,
        "market_cap_usd": 77_000,
        "price_pct_5m": 116,
        "txns_last_5m": 1500,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }
    decision = evaluate_research_rank_canary(token, {"rank_score": 75}, dry_run=True, live=False)

    assert decision.allowed
    assert decision.priority is True
    assert decision.reason == "research_rank_canary_priority"


def test_research_rank_canary_priority_requires_route_and_real_liquidity() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 22_000,
        "market_cap_usd": 77_000,
        "price_pct_5m": 116,
        "txns_last_5m": 1500,
        "has_jupiter_route": False,
        "liquidity_is_proxy": 0,
    }
    no_route = evaluate_research_rank_canary(token, {"rank_score": 75}, dry_run=True, live=False)
    assert not no_route.allowed

    token["has_jupiter_route"] = True
    token["liquidity_is_proxy"] = 1
    proxy = evaluate_research_rank_canary(token, {"rank_score": 75}, dry_run=True, live=False)
    assert not proxy.allowed


def test_research_rank_canary_pullback_is_shadow_by_default() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 22_000,
        "market_cap_usd": 74_000,
        "price_pct_5m": -4,
        "txns_last_5m": 350,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }

    decision = evaluate_research_rank_canary(token, {"rank_score": 73}, dry_run=True, live=False)

    assert not decision.allowed
    assert decision.shadow_as_own_lane is True
    assert decision.reason == "shadow_rank_canary"


def test_research_rank_canary_pullback_tail_micro_is_shadow_in_priority_only_mode() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 36_253,
        "market_cap_usd": 203_401,
        "price_pct_5m": -9.97,
        "txns_last_5m": 695,
        "volume_24h_usd": 486_535,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }

    decision = evaluate_research_rank_canary(token, {"rank_score": 71}, dry_run=True, live=False)

    assert not decision.allowed
    assert decision.shadow_as_own_lane is True
    assert decision.reason == "shadow_rank_canary"


def test_research_rank_canary_broad_normal_is_shadow_by_default() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 3_000,
        "market_cap_usd": 50_000,
        "price_pct_5m": 70,
        "txns_last_5m": 350,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }

    decision = evaluate_research_rank_canary(token, {"rank_score": 70}, dry_run=True, live=False)

    assert not decision.allowed
    assert decision.shadow_as_own_lane is True
    assert decision.reason == "shadow_rank_canary"


def test_research_rank_canary_blocks_stale_high_momentum_without_priority_strength() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 21_000,
        "market_cap_usd": 71_000,
        "price_pct_5m": 61,
        "txns_last_5m": 418,
        "age_minutes": 25,
        "queue_age_minutes": 8,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }

    decision = evaluate_research_rank_canary(token, {"rank_score": 76}, dry_run=True, live=False)

    assert not decision.allowed
    assert decision.shadow_as_own_lane is True
    assert decision.reason == "shadow_rank_canary"


def test_research_rank_priority_report_outputs_priority_vs_normal(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(
        "\n".join(
            [
                '{"address":"A","entry_lane":"pump_early_research_rank_canary","reason":"research_rank_canary_priority","total_pnl_pct":12}',
                '{"address":"B","entry_lane":"pump_early_research_rank_canary","reason":"research_rank_canary","total_pnl_pct":-3}',
            ]
        ),
        encoding="utf-8",
    )

    report = write_research_rank_priority_report(tmp_path)

    assert report["historical"]["priority"]["rows"] == 1
    assert report["historical"]["normal"]["rows"] == 1
    assert "elite_consolidation" in report["historical"]
    assert "pullback_tail_micro" in report["historical"]
