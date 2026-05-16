from __future__ import annotations

import json
from types import SimpleNamespace

from analytics.sniper_research_subprofiles import (
    SUBPROFILE_HIGH_ACTIVITY,
    SUBPROFILE_MOMENTUM_IGNITION,
    evaluate_sniper_research_subprofile,
    write_sniper_research_subprofile_report,
)


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        SNIPER_RESEARCH_SUBPROFILES_ENABLED=True,
        SNIPER_RESEARCH_HIGH_ACTIVITY_ENABLED=True,
        SNIPER_RESEARCH_HIGH_ACTIVITY_MIN_TXNS_5M=500,
        SNIPER_RESEARCH_HIGH_ACTIVITY_REQUIRE_ROUTE=True,
        SNIPER_RESEARCH_MOMENTUM_IGNITION_ENABLED=True,
        SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M=100,
        SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M=180,
        SNIPER_RESEARCH_MOMENTUM_MIN_LIQUIDITY_USD=10_000,
        SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M=100,
        SNIPER_RESEARCH_MOMENTUM_MAX_TXNS_5M=500,
        SNIPER_RESEARCH_MOMENTUM_MIN_MCAP_USD=10_000,
        SNIPER_RESEARCH_MOMENTUM_MAX_MCAP_USD=50_000,
    )


def test_high_activity_labels() -> None:
    decision = evaluate_sniper_research_subprofile(
        {
            "entry_lane": "pump_early_sniper_research",
            "dex_id": "pumpswap",
            "txns_last_5m": 600,
            "has_jupiter_route": True,
            "liquidity_is_proxy": False,
        },
        cfg=_cfg(),
    )

    assert decision.allowed is True
    assert decision.subprofile == SUBPROFILE_HIGH_ACTIVITY


def test_momentum_ignition_labels() -> None:
    decision = evaluate_sniper_research_subprofile(
        {
            "entry_lane": "pump_early_sniper_research",
            "dex_id": "pumpswap",
            "price_pct_5m": 140,
            "liquidity_usd": 12_000,
            "txns_last_5m": 250,
            "market_cap_usd": 25_000,
            "has_jupiter_route": True,
        },
        cfg=_cfg(),
    )

    assert decision.allowed is True
    assert decision.subprofile == SUBPROFILE_MOMENTUM_IGNITION


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
    assert decision.reason.startswith("sniper_research_subprofile_not_matched:")


def test_report_splits_pnl_by_subprofile(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    rows = [
        {
            "address": "A",
            "entry_lane": "pump_early_sniper_research",
            "dex_id": "pumpswap",
            "txns_last_5m": 650,
            "has_jupiter_route": True,
            "total_pnl_pct": 120,
        },
        {
            "address": "B",
            "entry_lane": "pump_early_sniper_research",
            "dex_id": "pumpswap",
            "price_pct_5m": 120,
            "liquidity_usd": 15_000,
            "txns_last_5m": 180,
            "market_cap_usd": 20_000,
            "has_jupiter_route": True,
            "total_pnl_pct": -5,
        },
    ]
    (metrics / "candidate_outcomes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    report = write_sniper_research_subprofile_report(tmp_path)

    assert report["by_subprofile"][SUBPROFILE_HIGH_ACTIVITY]["rows"] == 1
    assert report["by_subprofile"][SUBPROFILE_MOMENTUM_IGNITION]["rows"] == 1
