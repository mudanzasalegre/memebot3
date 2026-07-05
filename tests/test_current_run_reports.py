from __future__ import annotations

import json
import sqlite3

from analytics.current_run_reports import (
    write_bot_profitability_health,
    write_current_run_lane_summary,
    write_current_run_trade_diagnostics,
)


def test_current_run_reports_use_latest_run_id(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "runtime_events.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"event_type": "paper_buy", "address": "OLD", "run_id": "run-old", "run_started_at": "2026-05-21T10:00:00+00:00", "ts_utc": "2026-05-21T10:01:00+00:00", "entry_lane": "pump_early_research_rank_canary"}),
                json.dumps({"event_type": "paper_buy", "address": "NEW", "run_id": "run-new", "run_started_at": "2026-05-22T10:00:00+00:00", "ts_utc": "2026-05-22T10:01:00+00:00", "entry_lane": "pump_early_shadow_followup_micro"}),
            ]
        ),
        encoding="utf-8",
    )
    (metrics / "candidate_outcomes.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"address": "OLD-S", "run_id": "run-old", "run_started_at": "2026-05-21T10:00:00+00:00", "sample_type": "shadow_close", "target_total_pnl_pct": 600}),
                json.dumps({"address": "NEW-S", "run_id": "run-new", "run_started_at": "2026-05-22T10:00:00+00:00", "sample_type": "shadow_close", "target_total_pnl_pct": 75}),
                json.dumps({"address": "NEW", "run_id": "run-new", "run_started_at": "2026-05-22T10:00:00+00:00", "sample_type": "candidate_outcome", "target_total_pnl_pct": 600}),
            ]
        ),
        encoding="utf-8",
    )

    diag = write_current_run_trade_diagnostics(tmp_path)
    lanes = write_current_run_lane_summary(tmp_path)
    health = write_bot_profitability_health(tmp_path)

    assert diag["current_run"]["run_id"] == "run-new"
    assert diag["shadow_outcomes"]["rows"] == 1
    assert "pump_early_shadow_followup_micro" in lanes["lanes"]
    assert health["missed_peak_100_500_1000"]["peak_500"] == 0


def test_profitability_health_dedupes_portfolio_and_sqlite_positions(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "runtime_events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_type": "actual_paper_buy",
                        "address": "DUP",
                        "run_id": "run-dedupe",
                        "run_started_at": "2026-05-22T10:00:00+00:00",
                        "ts_utc": "2026-05-22T10:01:00+00:00",
                        "entry_lane": "pump_early_paper_bootstrap_micro",
                    }
                ),
                json.dumps(
                    {
                        "event_type": "buy",
                        "address": "DUP",
                        "run_id": "run-dedupe",
                        "run_started_at": "2026-05-22T10:00:00+00:00",
                        "ts_utc": "2026-05-22T10:01:00+00:00",
                        "entry_lane": "pump_early_paper_bootstrap_micro",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "data" / "paper_portfolio.json").write_text(
        json.dumps(
            {
                "positions": [
                    {
                        "address": "DUP",
                        "run_id": "run-dedupe",
                        "opened_at": "2026-05-22T10:01:00+00:00",
                        "closed": True,
                        "total_pnl_pct": -10,
                        "total_pnl_usd": -1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "data" / "memebotdatabase.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "create table positions (address text, run_id text, opened_at text, closed integer, total_pnl_pct real, total_pnl_usd real)"
    )
    conn.execute(
        "insert into positions values (?, ?, ?, ?, ?, ?)",
        ("DUP", "run-dedupe", "2026-05-22T10:01:00+00:00", 1, -10.0, -1.0),
    )
    conn.commit()
    conn.close()

    health = write_bot_profitability_health(tmp_path)

    assert health["current_run_closed_trades"] == 1
    assert health["current_run_total_usd"] == -1.0
    assert health["buys_per_hour"] == 60.0
