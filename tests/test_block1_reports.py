from __future__ import annotations

import json

from analytics.early_dump_cut_report import build_early_dump_cut_report
from analytics.green_sniper_restricted_report import build_green_sniper_restricted_report
from analytics.post_partial_protection_report import build_post_partial_protection_report
from analytics.research_rank_edge_report import build_research_rank_edge_report


def _write_rows(tmp_path, rows: list[dict]) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def test_research_rank_edge_report_does_not_mix_green_sniper(tmp_path) -> None:
    _write_rows(
        tmp_path,
        [
            {
                "address": "RANK",
                "entry_lane": "pump_early_research_rank_canary",
                "rank_score": 70,
                "market_cap_usd": 50_000,
                "price_pct_5m": 70,
                "liquidity_usd": 15_000,
                "pnl_pct": 100,
                "peak_pnl_pct": 120,
            },
            {
                "address": "GREEN",
                "entry_lane": "pump_early_green_candle_sniper",
                "pnl_pct": -40,
                "exit_reason": "LIQUIDITY_CRUSH",
            },
        ],
    )
    report = build_research_rank_edge_report(tmp_path)
    assert report["summary"]["trades"] == 1
    assert report["by_policy_category"]["research_rank_canary"]["trades"] == 1
    assert "green_sniper_pure" not in report["by_policy_category"]


def test_green_sniper_restricted_report_splits_eligible_and_ineligible(tmp_path) -> None:
    _write_rows(
        tmp_path,
        [
            {
                "address": "OK",
                "entry_lane": "pump_early_green_candle_sniper",
                "gate_profile": "green_sniper_restricted_buy",
                "rank_score": 70,
                "txns_last_5m": 350,
                "liquidity_usd": 15_000,
                "market_cap_usd": 50_000,
                "price_pct_5m": 70,
                "has_jupiter_route": 1,
                "pnl_pct": 40,
            },
            {
                "address": "BAD",
                "entry_lane": "pump_early_green_candle_sniper",
                "rank_score": 30,
                "txns_last_5m": 50,
                "liquidity_usd": 2_000,
                "market_cap_usd": 15_000,
                "price_pct_5m": 180,
                "has_jupiter_route": 0,
                "pnl_pct": -50,
                "exit_reason": "ADVERSE_TICK",
            },
        ],
    )
    report = build_green_sniper_restricted_report(tmp_path)
    assert report["restricted_eligible"]["trades"] == 1
    assert report["restricted_ineligible"]["trades"] == 1
    assert report["failure_counts"]["rank"] == 1


def test_early_dump_cut_report_exposes_search_space_and_loss_stats(tmp_path) -> None:
    _write_rows(
        tmp_path,
        [
            {"address": "A", "exit_reason": "EARLY_DUMP_CUT", "pnl_pct": -39, "peak_pnl_pct": 0},
            {"address": "B", "exit_reason": "EARLY_DUMP_CUT", "pnl_pct": -20, "peak_pnl_pct": 60},
        ],
    )
    report = build_early_dump_cut_report(tmp_path)
    assert report["summary"]["count"] == 2
    assert report["summary"]["avg_loss"] == -29.5
    assert report["summary"]["false_cut_runners"] == 1
    assert report["search_space"]["pnl_thresholds"] == [-8.0, -10.0, -12.0]
    assert report["candidates"][0]["saved_loss_estimate"] > 0


def test_post_partial_protection_report_compares_current_vs_protected(tmp_path) -> None:
    _write_rows(tmp_path, [{"address": "RUN", "pnl_pct": 20, "peak_pnl_pct": 100}])
    report = build_post_partial_protection_report(tmp_path)
    assert report["post_partial_protected"]["avg_pnl"] > report["current"]["avg_pnl"]
    assert report["delta"]["avg_pnl"] > 0
