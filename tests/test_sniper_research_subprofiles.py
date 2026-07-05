from __future__ import annotations

import json
from types import SimpleNamespace

from analytics.sniper_research_subprofiles import (
    SUBPROFILE_DEEP_REVERSAL,
    SUBPROFILE_MICRO_FALLBACK,
    SUBPROFILE_MOMENTUM_IGNITION,
    apply_sniper_research_subprofile_context,
    evaluate_sniper_research_subprofile,
    write_sniper_research_micro_fallback_report,
    write_sniper_research_subprofile_report,
)


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        SNIPER_RESEARCH_SUBPROFILES_ENABLED=True,
        SNIPER_RESEARCH_MICRO_FALLBACK_ENABLED=True,
        SNIPER_RESEARCH_MICRO_FALLBACK_LIVE_ENABLED=False,
        SNIPER_RESEARCH_MICRO_FALLBACK_AMOUNT_SOL=0.003,
        SNIPER_RESEARCH_MICRO_FALLBACK_MAX_OPEN=1,
        SNIPER_RESEARCH_MICRO_FALLBACK_MAX_DAILY_BUYS=5,
        SNIPER_RESEARCH_MICRO_FALLBACK_MIN_TXNS_5M=800,
        SNIPER_RESEARCH_MICRO_FALLBACK_MIN_LIQUIDITY_USD=10_000,
        SNIPER_RESEARCH_MICRO_FALLBACK_MAX_MCAP_USD=120_000,
        SNIPER_RESEARCH_MOMENTUM_IGNITION_ENABLED=True,
        SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M=100,
        SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M=150,
        SNIPER_RESEARCH_MOMENTUM_MIN_LIQUIDITY_USD=15_000,
        SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M=500,
        SNIPER_RESEARCH_MOMENTUM_MIN_MCAP_USD=15_000,
        SNIPER_RESEARCH_MOMENTUM_MAX_MCAP_USD=70_000,
        SNIPER_RESEARCH_MOMENTUM_MAX_TOP10_SHARE_PCT=40,
        SNIPER_RESEARCH_DEEP_REVERSAL_ENABLED=True,
        SNIPER_RESEARCH_DEEP_REVERSAL_MIN_PRICE5M=-90,
        SNIPER_RESEARCH_DEEP_REVERSAL_MAX_PRICE5M=-50,
        SNIPER_RESEARCH_DEEP_REVERSAL_MIN_TXNS_5M=500,
        SNIPER_RESEARCH_DEEP_REVERSAL_MAX_MCAP_USD=25_000,
    )


def test_momentum_ignition_labels() -> None:
    decision = evaluate_sniper_research_subprofile(
        {
            "entry_lane": "pump_early_sniper_research",
            "dex_id": "pumpswap",
            "price_pct_5m": 140,
            "liquidity_usd": 16_000,
            "txns_last_5m": 600,
            "market_cap_usd": 55_000,
            "has_jupiter_route": True,
            "trend": "up",
            "cluster_bad": False,
            "helius_top10_share_pct": 35,
        },
        cfg=_cfg(),
    )

    assert decision.allowed is True
    assert decision.subprofile == SUBPROFILE_MOMENTUM_IGNITION


def test_momentum_ignition_blocks_toxic_cluster_and_holder_concentration() -> None:
    decision = evaluate_sniper_research_subprofile(
        {
            "entry_lane": "pump_early_sniper_research",
            "dex_id": "pumpswap",
            "price_pct_5m": 140,
            "liquidity_usd": 16_000,
            "txns_last_5m": 600,
            "market_cap_usd": 55_000,
            "has_jupiter_route": True,
            "trend": "up",
            "cluster_bad": True,
            "helius_top10_share_pct": 44.59,
        },
        cfg=_cfg(),
    )

    assert decision.allowed is False
    assert decision.reason.startswith("momentum_ignition_toxic_filter:")
    assert "momentum:cluster_bad" in decision.failures
    assert "momentum:helius_top10_share>40" in decision.failures


def test_momentum_ignition_requires_trend_unless_second_tick_confirmed() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "price_pct_5m": 140,
        "liquidity_usd": 16_000,
        "txns_last_5m": 600,
        "market_cap_usd": 55_000,
        "has_jupiter_route": True,
        "trend": "unknown",
        "trend_fallback_used": True,
    }

    blocked = evaluate_sniper_research_subprofile(token, cfg=_cfg())
    assert blocked.allowed is False
    assert "momentum:trend_missing_without_second_tick" in blocked.failures
    assert blocked.reason == "momentum_ignition_needs_confirmation"

    token["second_tick_improved"] = True
    allowed = evaluate_sniper_research_subprofile(token, cfg=_cfg())
    assert allowed.allowed is True
    assert allowed.subprofile == SUBPROFILE_MOMENTUM_IGNITION


def test_momentum_ignition_trend_missing_allowed_with_strong_txns() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "price_pct_5m": 140,
        "liquidity_usd": 16_000,
        "txns_last_5m": 1500,
        "market_cap_usd": 55_000,
        "has_jupiter_route": True,
        "trend": "unknown",
        "trend_fallback_used": True,
    }

    decision = evaluate_sniper_research_subprofile(token, cfg=_cfg())

    assert decision.allowed is True
    assert decision.subprofile == SUBPROFILE_MOMENTUM_IGNITION


def test_momentum_ignition_trend_missing_allowed_with_strong_rank() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "price_pct_5m": 140,
        "liquidity_usd": 16_000,
        "txns_last_5m": 600,
        "market_cap_usd": 55_000,
        "has_jupiter_route": True,
        "rank_score": 75,
        "trend": "unknown",
        "trend_fallback_used": True,
    }

    decision = evaluate_sniper_research_subprofile(token, cfg=_cfg())

    assert decision.allowed is True
    assert decision.subprofile == SUBPROFILE_MOMENTUM_IGNITION


def test_momentum_ignition_cluster_and_toxic_stay_hard_shadow() -> None:
    base = {
        "entry_lane": "pump_early_sniper_research",
        "price_pct_5m": 140,
        "liquidity_usd": 30_000,
        "txns_last_5m": 1500,
        "market_cap_usd": 55_000,
        "has_jupiter_route": True,
        "trend": "unknown",
        "trend_fallback_used": True,
    }
    cluster = evaluate_sniper_research_subprofile({**base, "cluster_bad": True}, cfg=_cfg())
    toxic = evaluate_sniper_research_subprofile({**base, "toxic_initial_sell_pressure": True}, cfg=_cfg())

    assert cluster.allowed is False
    assert "momentum:cluster_bad" in cluster.failures
    assert toxic.allowed is False
    assert "momentum:toxic_initial_sell_pressure" in toxic.failures


def test_deep_reversal_labels_and_sets_defensive_exit() -> None:
    token = {
        "entry_lane": "pump_early_sniper_research",
        "price_pct_5m": -72,
        "txns_last_5m": 650,
        "market_cap_usd": 20_000,
        "has_jupiter_route": True,
    }
    decision = evaluate_sniper_research_subprofile(
        token,
        cfg=_cfg(),
    )
    apply_sniper_research_subprofile_context(token, decision)

    assert decision.allowed is True
    assert decision.subprofile == SUBPROFILE_DEEP_REVERSAL
    assert token["entry_subprofile"] == SUBPROFILE_DEEP_REVERSAL
    assert token["exit_profile"] == "sniper_deep_reversal_defensive"
    assert "sniper_research_defensive_exit" not in token


def test_unmatched_sniper_research_goes_shadow() -> None:
    decision = evaluate_sniper_research_subprofile(
        {
            "entry_lane": "pump_early_sniper_research",
            "dex_id": "raydium",
            "txns_last_5m": 25,
            "has_jupiter_route": False,
        },
        cfg=_cfg(),
    )

    assert decision.allowed is False
    assert decision.reason.startswith("sniper_research_micro_fallback_not_matched:")


def _micro_candidate(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "entry_lane": "pump_early_sniper_research",
        "dex_id": "pumpswap",
        "price_pct_5m": 60,
        "liquidity_usd": 12_000,
        "txns_last_5m": 900,
        "market_cap_usd": 80_000,
        "has_jupiter_route": True,
        "price_usd": 0.0001,
        "cluster_bad": False,
        "toxic_initial_sell_pressure": False,
        "trend": "unknown",
        "trend_fallback_used": True,
    }
    row.update(overrides)
    return row


def test_sniper_research_micro_fallback_strong_candidate_allowed() -> None:
    token = _micro_candidate()
    decision = evaluate_sniper_research_subprofile(token, cfg=_cfg())
    apply_sniper_research_subprofile_context(token, decision)

    assert decision.allowed is True
    assert decision.subprofile == SUBPROFILE_MICRO_FALLBACK
    assert token["entry_lane"] == "pump_early_sniper_research_micro_fallback"
    assert token["profit_lane_tier"] == "pump_early_sniper_research_micro_fallback"
    assert token["gate_profile"] == "sniper_research_micro_fallback"
    assert token["lane_policy_category"] == "sniper_research_micro_fallback"
    assert token["amount_sol"] == 0.003


def test_sniper_research_micro_fallback_honors_configured_amount_without_hidden_cap() -> None:
    cfg = _cfg()
    cfg.SNIPER_RESEARCH_MICRO_FALLBACK_AMOUNT_SOL = 0.02
    token = _micro_candidate()
    decision = evaluate_sniper_research_subprofile(token, cfg=cfg)
    apply_sniper_research_subprofile_context(token, decision)

    assert decision.allowed is True
    assert decision.subprofile == SUBPROFILE_MICRO_FALLBACK
    assert token["amount_sol"] == 0.02


def test_sniper_research_micro_fallback_toxic_blocked() -> None:
    decision = evaluate_sniper_research_subprofile(
        _micro_candidate(toxic_initial_sell_pressure=True),
        cfg=_cfg(),
    )

    assert decision.allowed is False
    assert decision.reason.startswith("sniper_research_micro_fallback_not_matched:")
    assert "micro_fallback:toxic_initial_sell_pressure" in decision.failures


def test_sniper_research_micro_fallback_cluster_bad_blocked() -> None:
    decision = evaluate_sniper_research_subprofile(_micro_candidate(cluster_bad=True), cfg=_cfg())

    assert decision.allowed is False
    assert "micro_fallback:cluster_bad" in decision.failures


def test_sniper_research_micro_fallback_high_mcap_blocked() -> None:
    decision = evaluate_sniper_research_subprofile(_micro_candidate(market_cap_usd=150_000), cfg=_cfg())

    assert decision.allowed is False
    assert "micro_fallback:mcap>120000" in decision.failures


def test_sniper_research_micro_fallback_no_route_blocked() -> None:
    decision = evaluate_sniper_research_subprofile(_micro_candidate(has_jupiter_route=False), cfg=_cfg())

    assert decision.allowed is False
    assert "micro_fallback:route_required" in decision.failures


def test_report_splits_pnl_by_subprofile(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    rows = [
        {
            "address": "A",
            "entry_lane": "pump_early_sniper_research",
            "dex_id": "pumpswap",
            "price_pct_5m": -70,
            "txns_last_5m": 650,
            "market_cap_usd": 20_000,
            "has_jupiter_route": True,
            "total_pnl_pct": 120,
        },
        {
            "address": "B",
            "entry_lane": "pump_early_sniper_research",
            "dex_id": "pumpswap",
            "price_pct_5m": 120,
            "liquidity_usd": 15_000,
            "txns_last_5m": 600,
            "market_cap_usd": 20_000,
            "has_jupiter_route": True,
            "trend": "up",
            "total_pnl_pct": -5,
        },
    ]
    (metrics / "candidate_outcomes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    report = write_sniper_research_subprofile_report(tmp_path)

    assert report["by_subprofile"][SUBPROFILE_DEEP_REVERSAL]["rows"] == 1
    assert report["by_subprofile"][SUBPROFILE_MOMENTUM_IGNITION]["rows"] == 1


def test_micro_fallback_report_fields(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    rows = [
        {
            **_micro_candidate(address="A"),
            "entry_lane": "pump_early_sniper_research_micro_fallback",
            "gate_profile": "sniper_research_micro_fallback",
            "event_type": "sniper_research_micro_fallback_buy",
            "amount_sol": 0.003,
            "total_pnl_pct": 25,
            "highest_pnl_pct": 120,
        },
        {
            **_micro_candidate(address="B", market_cap_usd=150_000),
            "reason": "sniper_research_micro_fallback_not_matched",
        },
    ]
    (metrics / "runtime_events.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    report = write_sniper_research_micro_fallback_report(tmp_path)

    assert set(report) >= {
        "seen",
        "allowed",
        "bought",
        "shadowed",
        "avg_pnl",
        "peak100_count",
        "peak500_count",
        "severe_loss_count",
    }
    assert report["allowed"] >= 1
    assert report["bought"] == 1
    assert report["peak100_count"] == 1
