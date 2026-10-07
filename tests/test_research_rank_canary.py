from __future__ import annotations

import json

from analytics.research_rank_canary import (
    apply_research_rank_canary_context,
    apply_research_rank_canary_shadow_context,
    evaluate_research_rank_canary,
    write_research_rank_canary_audit_report,
    write_research_rank_current_run_report,
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
    assert decision.amount_sol == 0.02


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
    assert decision.shadow_as_own_lane is True


def test_research_rank_canary_rejects_proxy_liquidity() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 22_000,
        "market_cap_usd": 50_000,
        "price_pct_5m": 70,
        "txns_last_5m": 1200,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 1,
    }
    decision = evaluate_research_rank_canary(token, {"rank_score": 70}, dry_run=True, live=False)
    assert not decision.allowed
    assert decision.shadow_as_own_lane is True
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
    assert blocked.reason == "price5m_40_50_requires_rank70_or_liq20k"

    allowed_by_rank = evaluate_research_rank_canary(token, {"rank_score": 70}, dry_run=True, live=False)
    assert not allowed_by_rank.allowed
    assert allowed_by_rank.reason == "research_rank_canary_not_executable:liquidity_below_min"

    token["liquidity_usd"] = 20_000
    allowed_by_liq = evaluate_research_rank_canary(token, {"rank_score": 66}, dry_run=True, live=False)
    assert allowed_by_liq.allowed
    assert allowed_by_liq.reason == "research_rank_canary_paper_normal"
    assert allowed_by_liq.amount_sol == 0.005


def test_research_rank_canary_elite_consolidation_opens_after_priority_only_reopen() -> None:
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

    assert decision.allowed
    assert decision.elite_consolidation is True
    assert decision.reason == "research_rank_canary_elite_consolidation"


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
    assert decision.amount_sol == 0.02


def test_research_rank_canary_paper_normal_buys_micro() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "liquidity_usd": 16_000,
        "market_cap_usd": 32_992,
        "price_pct_5m": 115,
        "txns_last_5m": 350,
        "age_minutes": 20.8,
        "queue_age_minutes": 7.7,
        "has_jupiter_route": True,
        "liquidity_is_proxy": 0,
    }

    decision = evaluate_research_rank_canary(token, {"rank_score": 65.42}, dry_run=True, live=False)

    assert decision.allowed
    assert decision.reason == "research_rank_canary_paper_normal"
    assert decision.amount_sol == 0.005


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
    assert no_route.shadow_as_own_lane is True

    token["has_jupiter_route"] = True
    token["liquidity_is_proxy"] = 1
    proxy = evaluate_research_rank_canary(token, {"rank_score": 75}, dry_run=True, live=False)
    assert not proxy.allowed
    assert proxy.shadow_as_own_lane is True


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
    assert decision.reason == "research_rank_canary_pullback_shadow_only"


def test_research_rank_canary_pullback_tail_micro_remains_small_paper_lane() -> None:
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

    assert decision.allowed
    assert decision.pullback_tail_micro is True
    assert decision.reason == "research_rank_canary_pullback_tail_micro"
    assert 0.0 < decision.amount_sol <= 0.005


def test_research_rank_canary_low_liquidity_normal_shadows() -> None:
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
    assert decision.reason == "research_rank_canary_not_executable:liquidity_below_min"


def test_research_rank_canary_stale_high_momentum_normal_micro_still_buys() -> None:
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

    assert decision.allowed
    assert decision.reason == "research_rank_canary_paper_normal"
    assert decision.amount_sol == 0.005


def test_research_rank_priority_report_outputs_priority_vs_normal(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(
        "\n".join(
            [
                '{"address":"A","entry_lane":"pump_early_research_rank_canary","reason":"research_rank_canary_priority","total_pnl_pct":12}',
                '{"address":"B","entry_lane":"pump_early_research_rank_canary","reason":"research_rank_canary_paper_normal","total_pnl_pct":-3}',
                '{"address":"C","entry_lane":"pump_early_research_rank_canary","reason":"research_rank_canary_normal_shadow_only","action":"shadow"}',
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "data" / "paper_portfolio.json").write_text(
        json.dumps(
            {
                "positions": [
                    {
                        "address": "A",
                        "entry_lane": "pump_early_research_rank_canary",
                        "reason": "research_rank_canary_priority",
                        "opened_at": "2026-05-22T10:00:00+00:00",
                    },
                    {
                        "address": "B",
                        "entry_lane": "pump_early_research_rank_canary",
                        "reason": "research_rank_canary_paper_normal",
                        "opened_at": "2026-05-22T10:01:00+00:00",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    report = write_research_rank_priority_report(tmp_path)

    assert report["historical"]["priority"]["rows"] == 1
    assert report["historical"]["normal"]["rows"] == 2
    assert report["priority_seen"] == 1
    assert report["priority_bought"] == 1
    assert report["normal_micro_seen"] == 2
    assert report["normal_micro_bought"] == 1
    assert report["normal_shadow"] == 1
    assert report["priority_shadow"] == 0
    assert "elite_consolidation" in report["historical"]
    assert "pullback_tail_micro" in report["historical"]


def test_research_rank_reports_do_not_treat_shadow_pnl_as_buys(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "runtime_events.jsonl").write_text(
        '{"event_type":"heartbeat","run_id":"rank-run","run_started_at":"2026-05-22T10:00:00+00:00","ts_utc":"2026-05-22T10:01:00+00:00"}\n',
        encoding="utf-8",
    )
    (metrics / "candidate_outcomes.jsonl").write_text(
        "\n".join(
            [
                '{"event_type":"candidate_outcome","run_id":"rank-run","address":"A","entry_lane":"pump_early_research_rank_canary","reason":"research_rank_canary_priority","pnl_pct":12,"outcome":"closed"}',
                '{"event_type":"candidate_outcome","run_id":"rank-run","address":"B","entry_lane":"pump_early_research_rank_canary","reason":"research_rank_canary_paper_normal","pnl_pct":4,"outcome":"closed"}',
            ]
        ),
        encoding="utf-8",
    )

    priority = write_research_rank_priority_report(tmp_path)
    current = write_research_rank_current_run_report(tmp_path)
    audit = write_research_rank_canary_audit_report(tmp_path)

    for report in (priority, current, audit):
        assert report["priority_bought"] == 0
        assert report["normal_micro_bought"] == 0
    assert current["current_run_rank_trades"]["rows"] == 0


def test_research_rank_reports_expose_pr06_counters(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(
        "\n".join(
            [
                '{"address":"A","entry_lane":"pump_early_research_rank_canary","reason":"research_rank_canary_priority","total_pnl_pct":12}',
                '{"address":"B","entry_lane":"pump_early_research_rank_canary","reason":"research_rank_canary_paper_normal","total_pnl_pct":4}',
                '{"address":"C","entry_lane":"pump_early_research_rank_canary","reason":"research_rank_canary_not_executable:no_route_paper","action":"shadow"}',
            ]
        ),
        encoding="utf-8",
    )

    audit = write_research_rank_canary_audit_report(tmp_path)
    current = write_research_rank_current_run_report(tmp_path)

    for report in (audit, current):
        for field in (
            "normal_micro_seen",
            "normal_micro_bought",
            "priority_seen",
            "priority_bought",
            "normal_shadow",
            "priority_shadow",
        ):
            assert field in report
