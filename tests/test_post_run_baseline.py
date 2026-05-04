from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from analytics.post_run_baseline import build_post_run_baseline


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _create_positions_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY,
                address TEXT,
                symbol TEXT,
                closed INTEGER,
                closed_at TEXT,
                entry_regime TEXT,
                entry_lane TEXT,
                gate_profile TEXT,
                exit_reason TEXT,
                total_pnl_pct REAL,
                total_pnl_usd REAL,
                highest_pnl_pct REAL,
                max_pnl_pct_seen REAL,
                buy_price_pct_5m REAL,
                buy_market_cap_usd REAL,
                buy_liquidity_usd REAL,
                buy_liquidity_is_proxy INTEGER
            )
            """
        )
        rows = [
            (1, "BASE", "BASE", 1, "2026-05-01T12:36:21+00:00", "pump_early", "baseline_lane", "baseline", "STOP_LOSS", 10.0, 1.0, 10.0, 10.0, 10.0, 10_000.0, 5_000.0, 0),
            (2, "WIN", "WIN", 1, "2026-05-01T13:00:00+00:00", "pump_early", "pump_early_sniper_research", "pumpswap_profit_research", "POST_PARTIAL_TRAILING", 100.0, 10.0, 150.0, 150.0, 60.0, 50_000.0, 15_000.0, 0),
            (3, "LOSS", "LOSS", 1, "2026-05-01T14:00:00+00:00", "pump_early", "pump_early_green_candle_sniper", "green_sniper", "LIQUIDITY_CRUSH", -30.0, -3.0, 0.0, 0.0, 20.0, 20_000.0, 3_000.0, 1),
            (4, "LATE", "LATE", 1, "2026-05-01T15:00:00+00:00", "pump_early", None, None, "NO_PUMP_EXIT", -5.0, -0.5, 600.0, 600.0, None, None, None, None),
        ]
        conn.executemany(
            """
            INSERT INTO positions (
                id, address, symbol, closed, closed_at, entry_regime, entry_lane,
                gate_profile, exit_reason, total_pnl_pct, total_pnl_usd,
                highest_pnl_pct, max_pnl_pct_seen, buy_price_pct_5m,
                buy_market_cap_usd, buy_liquidity_usd, buy_liquidity_is_proxy
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def test_post_run_baseline_uses_forward_window_and_enrichment(tmp_path: Path) -> None:
    _create_positions_db(tmp_path / "data" / "memebotdatabase.db")
    _write_json(
        tmp_path / "data" / "metrics" / "post_partial_experiment.state.json",
        {
            "baseline_closed_count": 1,
            "baseline_latest_closed_at": "2026-05-01T12:36:21+00:00",
            "baseline_row_keys": ["db:1"],
        },
    )
    _write_jsonl(
        tmp_path / "data" / "metrics" / "candidate_outcomes.jsonl",
        [
            {
                "address": "WIN",
                "profit_lane_tier": "pump_early_research_rank_canary",
                "rank_score": 70,
            },
            {
                "address": "LATE",
                "entry_lane": "pump_early_late_momentum_watch",
                "profit_lane_tier": "pump_early_late_momentum_watch",
                "rank_score": 30,
                "price_pct_5m": 400,
                "market_cap_usd": 40_000,
                "liquidity_usd": 12_000,
                "liquidity_is_proxy": 0,
            },
        ],
    )

    report = build_post_run_baseline(tmp_path)

    assert report["source"]["primary"] == "sqlite"
    assert report["window"]["raw_closed_count"] == 4
    assert report["window"]["included_closed_count"] == 3
    assert report["global"]["count"] == 3
    assert report["global"]["win_rate_pct"] == 33.333
    assert report["global"]["avg_pnl_pct"] == 21.667
    assert report["global"]["median_pnl_pct"] == -5.0
    assert report["global"]["total_pnl_usd"] == 6.5
    assert report["global"]["severe_loss_count"] == 1
    assert report["global"]["runner_count_100"] == 2
    assert report["global"]["runner_count_500"] == 1
    assert report["by_research_sublane"]["pump_early_research_rank_canary"]["count"] == 1
    assert report["by_entry_lane"]["pump_early_late_momentum_watch"]["count"] == 1
    assert report["by_exit_reason"]["LIQUIDITY_CRUSH"]["severe_loss_count"] == 1
    assert report["by_rank_bucket"]["rank_61_75"]["count"] == 1
    assert report["by_price5m_bucket"]["price5m_300+"]["count"] == 1
    assert report["by_mcap_bucket"]["mcap_50k_100k"]["count"] == 1
    assert report["by_liquidity_bucket"]["liquidity_10k_25k"]["count"] == 2
    assert report["by_liquidity_proxy"]["proxy"]["count"] == 1
    assert report["severe_losses"][0]["trade_id"] == 3


def test_post_run_baseline_falls_back_to_paper_when_sqlite_unavailable(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "data" / "paper_portfolio.json",
        {
            "PAPER": {
                "token_address": "PAPER",
                "symbol": "PAPER",
                "closed": True,
                "closed_at": "2026-05-01T13:00:00+00:00",
                "entry_regime": "pump_early",
                "entry_lane": "pump_early_sniper_research",
                "exit_reason": "POST_PARTIAL_TRAILING",
                "total_pnl_pct": 42.0,
                "total_pnl_usd": 4.2,
                "max_pnl_pct_seen": 80.0,
            }
        },
    )

    report = build_post_run_baseline(tmp_path)

    assert report["source"]["primary"] == "paper_portfolio"
    assert report["global"]["count"] == 1
    assert report["global"]["avg_pnl_pct"] == 42.0
