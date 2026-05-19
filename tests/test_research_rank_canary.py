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
    assert decision.reason == "price5m_below_min"
    assert token["entry_lane"] == "pump_early_research_rank_canary"
    assert token["research_rank_canary_shadow"] == 1


def test_research_rank_canary_price5m_40_50_requires_rank70_or_liq20k() -> None:
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
    assert blocked.reason == "price5m_40_50_requires_rank70_or_liq20k"

    allowed_by_rank = evaluate_research_rank_canary(token, {"rank_score": 70}, dry_run=True, live=False)
    assert allowed_by_rank.allowed

    token["liquidity_usd"] = 20_000
    allowed_by_liq = evaluate_research_rank_canary(token, {"rank_score": 66}, dry_run=True, live=False)
    assert allowed_by_liq.allowed


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
