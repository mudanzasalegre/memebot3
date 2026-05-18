from __future__ import annotations

import json
from pathlib import Path

from analytics.post_hotfix_strategy_preview import write_post_hotfix_strategy_preview


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_post_hotfix_strategy_preview_writes_combined_artifacts(tmp_path: Path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    portfolio = {
        "positions": [
            {
                "address": "blocked-prime",
                "closed": True,
                "entry_lane": "pump_early_pumpswap_profit",
                "gate_profile": "pumpswap_profit_prime",
                "txns_last_5m": 200,
                "liquidity_usd": 8_000,
                "liquidity_usd_is_proxy": 0,
                "has_jupiter_route": 1,
                "price_impact_pct": 5,
                "total_pnl_pct": -35,
                "max_pnl_pct_seen": 10,
                "exit_reason": "ADVERSE_TICK",
            },
            {
                "address": "runner",
                "closed": True,
                "entry_lane": "pump_early_sniper_research",
                "gate_profile": "pumpswap_profit_research",
                "total_pnl_pct": 40,
                "max_pnl_pct_seen": 300,
            },
        ]
    }
    (tmp_path / "data" / "paper_portfolio.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "paper_portfolio.json").write_text(json.dumps(portfolio), encoding="utf-8")
    birth_rows = [
        {
            "address": f"birth-{idx}",
            "reason": "green_sniper:paper_birth_probe:proxy_liquidity_productive_block,low_txns_5m",
            "total_pnl_pct": 8,
            "max_pnl_pct_seen": 150 if idx < 3 else 20,
        }
        for idx in range(50)
    ]
    _write_jsonl(
        metrics / "candidate_outcomes.jsonl",
        [
            {
                "address": "rebound-new",
                "dex_id": "pumpswap",
                "price_pct_5m": -30,
                "txns_last_5m": 700,
                "liquidity_usd": 12_000,
                "market_cap_usd": 20_000,
                "liquidity_usd_is_proxy": 0,
                "has_jupiter_route": 1,
                "price_impact_pct": 4,
                "price_recovered_pct": 12,
                "total_pnl_pct": 25,
                "max_pnl_pct_seen": 120,
            },
            *birth_rows,
        ],
    )

    report = write_post_hotfix_strategy_preview(tmp_path)

    assert report["baseline_current"]["count"] == 2
    assert report["pumpswap_strict"]["preview"]["strict_blocked_current"]["count"] == 1
    assert report["rebound_lane"]["preview"]["incremental_candidates"]["count"] == 1
    assert report["birth_probe_micro_canary"]["candidate_rows"]["count"] == 50
    assert report["multi_partial_runner"]["rows"] >= 1
    assert "expected_total_pnl_delta_pct_points" in report["combined_hotfix_v1"]
    assert (metrics / "post_hotfix_strategy_preview.json").exists()
    assert (tmp_path / "docs" / "POST_HOTFIX_STRATEGY_PREVIEW.md").exists()
