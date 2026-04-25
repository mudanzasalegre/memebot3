from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

import pytest

from analytics.audit import build_audit_snapshot, build_trade_consistency
from api.services.trades import get_closed_trades_envelope
from api.settings import APISettings


UTC = dt.timezone.utc


def _write_positions_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY,
                address TEXT NOT NULL,
                symbol TEXT,
                opened_at TEXT,
                closed_at TEXT,
                closed INTEGER,
                qty INTEGER,
                entry_qty INTEGER,
                buy_price_usd REAL,
                entry_notional_usd REAL,
                realized_qty INTEGER,
                realized_proceeds_usd REAL,
                close_price_usd REAL,
                exit_reason TEXT,
                entry_regime TEXT,
                partial_taken INTEGER,
                buy_amount_sol REAL,
                size_bucket TEXT,
                size_multiplier REAL,
                price_source_at_buy TEXT,
                price_source_at_close TEXT,
                highest_pnl_pct REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE tokens (
                address TEXT PRIMARY KEY,
                symbol TEXT,
                created_at TEXT,
                discovered_at TEXT
            )
            """
        )

        rows = [
            (
                1,
                "addr-1",
                "AAA",
                "2026-04-10T08:00:00+00:00",
                "2026-04-10T09:00:00+00:00",
                1,
                0,
                10,
                1.0,
                10.0,
                10,
                12.0,
                1.2,
                "TAKE_PROFIT",
                "pump_early",
                0,
                0.10,
                "base",
                1.0,
                "jupiter",
                "jupiter",
                25.0,
            ),
            (
                2,
                "addr-2",
                "BBB",
                "2026-04-10T08:30:00+00:00",
                "2026-04-10T10:00:00+00:00",
                1,
                0,
                10,
                1.0,
                10.0,
                10,
                9.0,
                0.9,
                "STOP_LOSS",
                "pump_early",
                0,
                0.10,
                "base",
                1.0,
                "jupiter",
                "jupiter",
                5.0,
            ),
            (
                3,
                "addr-3",
                "CCC",
                "2026-04-10T08:45:00+00:00",
                "2026-04-10T10:00:00+00:00",
                1,
                0,
                10,
                1.0,
                10.0,
                10,
                11.0,
                1.1,
                "TAKE_PROFIT",
                "pump_early",
                1,
                0.10,
                "boosted",
                1.2,
                "jupiter",
                "jupiter",
                18.0,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO positions (
                id, address, symbol, opened_at, closed_at, closed, qty, entry_qty, buy_price_usd, entry_notional_usd,
                realized_qty, realized_proceeds_usd, close_price_usd, exit_reason, entry_regime, partial_taken,
                buy_amount_sol, size_bucket, size_multiplier, price_source_at_buy, price_source_at_close, highest_pnl_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.executemany(
            "INSERT INTO tokens (address, symbol, created_at, discovered_at) VALUES (?, ?, ?, ?)",
            [
                ("addr-1", "AAA", "2026-04-10T07:30:00+00:00", "2026-04-10T07:31:00+00:00"),
                ("addr-2", "BBB", "2026-04-10T07:40:00+00:00", "2026-04-10T07:41:00+00:00"),
                ("addr-3", "CCC", "2026-04-10T07:50:00+00:00", "2026-04-10T07:51:00+00:00"),
            ],
        )
        conn.commit()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _make_settings(tmp_path: Path, db_path: Path, paper_path: Path, scorecard_path: Path) -> APISettings:
    data_dir = tmp_path / "data"
    metrics_dir = data_dir / "metrics"
    runtime_dir = data_dir / "runtime"
    logs_dir = tmp_path / "logs"
    features_dir = tmp_path / "features"
    runtime_events_path = metrics_dir / "runtime_events.jsonl"
    research_events_path = metrics_dir / "candidate_outcomes.jsonl"
    research_thresholds_json = metrics_dir / "research_thresholds.json"
    recommended_threshold_json = metrics_dir / "recommended_threshold.json"
    train_status_json = metrics_dir / "train_status.json"
    dataset_quality_json = metrics_dir / "dataset_quality.json"
    bot_process_state_path = runtime_dir / "bot_process_state.json"
    bot_process_console_log_path = logs_dir / "bot_process_console.log"

    for directory in (data_dir, metrics_dir, runtime_dir, logs_dir, features_dir):
        directory.mkdir(parents=True, exist_ok=True)
    runtime_events_path.write_text("", encoding="utf-8")
    research_events_path.write_text("", encoding="utf-8")

    return APISettings(
        title="test",
        version="0.1.0",
        project_root=tmp_path,
        data_dir=data_dir,
        runtime_dir=runtime_dir,
        metrics_dir=metrics_dir,
        logs_dir=logs_dir,
        db_path=db_path,
        features_dir=features_dir,
        runtime_events_path=runtime_events_path,
        research_events_path=research_events_path,
        research_scorecard_json=scorecard_path,
        research_thresholds_json=research_thresholds_json,
        recommended_threshold_json=recommended_threshold_json,
        train_status_json=train_status_json,
        dataset_quality_json=dataset_quality_json,
        paper_portfolio_path=paper_path,
        bot_process_state_path=bot_process_state_path,
        bot_process_console_log_path=bot_process_console_log_path,
        auth_mode="local",
        session_cookie_name="test",
        session_ttl_seconds=3600,
        session_secret="secret",
        session_cookie_secure=False,
        local_auth_users=(),
        using_default_local_auth_users=False,
    )


def test_closed_trades_envelope_uses_total_count_and_stable_cursor(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.sqlite"
    _write_positions_db(db_path)
    paper_path = tmp_path / "data" / "paper_portfolio.json"
    scorecard_path = tmp_path / "data" / "metrics" / "research_scorecard.json"
    _write_json(
        paper_path,
        {
            "addr-1": {"closed": True, "total_pnl_usd": 2.0},
            "addr-2": {"closed": True, "total_pnl_usd": -1.0},
            "addr-3": {"closed": True, "total_pnl_usd": 1.0},
        },
    )
    _write_json(
        scorecard_path,
        {"generated_at_utc": "2026-04-10T10:30:00+00:00", "live_closed": 3},
    )
    settings = _make_settings(tmp_path, db_path, paper_path, scorecard_path)

    first_page = get_closed_trades_envelope(settings, limit=2).data
    assert first_page["total_count"] == 3
    assert first_page["page_count"] == 2
    assert first_page["has_more"] is True
    assert [item["trade_id"] for item in first_page["items"]] == [3, 2]
    assert first_page["next_before_ts"] == "2026-04-10T10:00:00+00:00"
    assert first_page["next_before_id"] == 2
    assert first_page["summary"]["closed_count"] == 3
    assert first_page["summary"]["total_pnl_usd"] == pytest.approx(2.0)
    assert first_page["summary"]["avg_pnl_pct"] == pytest.approx(6.6666666667)
    assert first_page["summary"]["median_pnl_pct"] == pytest.approx(10.0)

    second_page = get_closed_trades_envelope(
        settings,
        limit=2,
        before_ts=first_page["next_before_ts"],
        before_id=first_page["next_before_id"],
    ).data
    assert second_page["total_count"] == 3
    assert second_page["page_count"] == 1
    assert second_page["has_more"] is False
    assert [item["trade_id"] for item in second_page["items"]] == [1]
    assert second_page["summary"]["closed_count"] == 3
    assert {item["trade_id"] for item in first_page["items"]}.isdisjoint(
        {item["trade_id"] for item in second_page["items"]}
    )


def test_build_trade_consistency_flags_scorecard_lag_and_matching_pnl(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.sqlite"
    _write_positions_db(db_path)
    paper_path = tmp_path / "data" / "paper_portfolio.json"
    scorecard_path = tmp_path / "data" / "metrics" / "research_scorecard.json"
    _write_json(
        paper_path,
        {
            "addr-1": {"closed": True, "total_pnl_usd": 2.0},
            "addr-2": {"closed": True, "total_pnl_usd": -1.0},
            "addr-3": {"closed": True, "total_pnl_usd": 1.0},
        },
    )
    _write_json(
        scorecard_path,
        {"generated_at_utc": "2026-04-10T09:30:00+00:00", "live_closed": 2},
    )

    consistency = build_trade_consistency(
        db_path=db_path,
        paper_portfolio_path=paper_path,
        research_scorecard_path=scorecard_path,
    )

    assert consistency["db_closed_rows"] == 3
    assert consistency["paper_closed_rows"] == 3
    assert consistency["scorecard_live_closed"] == 2
    assert consistency["lag_rows"] == 1
    assert consistency["paper_matches_db"] is True
    assert consistency["pnl_matches_db"] is True
    assert consistency["db_total_pnl_usd"] == pytest.approx(2.0)
    assert consistency["paper_total_pnl_usd"] == pytest.approx(2.0)
    assert consistency["scorecard_stale_vs_latest_close"] is True
    assert consistency["is_consistent"] is False


def test_build_audit_snapshot_marks_stale_artifacts_and_normalizes_candidate_events(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.sqlite"
    _write_positions_db(db_path)
    paper_path = tmp_path / "data" / "paper_portfolio.json"
    scorecard_path = tmp_path / "data" / "metrics" / "research_scorecard.json"
    research_events_path = tmp_path / "data" / "metrics" / "candidate_outcomes.jsonl"
    edge_report_path = tmp_path / "docs" / "EDGE_REPORT.md"
    ml_report_path = tmp_path / "docs" / "ML_REPORT.md"

    _write_json(
        paper_path,
        {
            "addr-1": {"closed": True, "total_pnl_usd": 2.0},
            "addr-2": {"closed": True, "total_pnl_usd": -1.0},
            "addr-3": {"closed": True, "total_pnl_usd": 1.0},
        },
    )
    _write_json(
        scorecard_path,
        {"generated_at_utc": "2026-04-10T09:30:00+00:00", "live_closed": 2},
    )

    research_events_path.parent.mkdir(parents=True, exist_ok=True)
    research_events_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts_utc": "2026-04-10T08:00:00+00:00",
                        "event_type": "candidate_decision",
                        "address": "addr-1",
                        "decision_action": "bought",
                        "regime": "pump",
                    }
                ),
                json.dumps(
                    {
                        "ts_utc": "2026-04-10T08:01:00+00:00",
                        "event_type": "candidate_decision",
                        "address": "addr-1",
                        "decision_action": "bought",
                        "regime": "pump_early",
                    }
                ),
                json.dumps(
                    {
                        "ts_utc": "2026-04-10T10:00:00+00:00",
                        "event_type": "candidate_outcome",
                        "address": "addr-1",
                        "pnl_pct": 20.0,
                        "source": "live_trade",
                        "regime": "pump_early",
                        "label": 1,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    edge_report_path.parent.mkdir(parents=True, exist_ok=True)
    edge_report_path.write_text("# edge\n", encoding="utf-8")
    ml_report_path.write_text("# ml\n", encoding="utf-8")
    stale_ts = dt.datetime(2026, 4, 10, 8, 0, tzinfo=UTC).timestamp()
    os.utime(edge_report_path, (stale_ts, stale_ts))
    os.utime(ml_report_path, (stale_ts, stale_ts))

    snapshot = build_audit_snapshot(
        db_path=db_path,
        features_dir=tmp_path / "features",
        runtime_events_path=tmp_path / "data" / "metrics" / "runtime_events.jsonl",
        research_events_path=research_events_path,
        paper_portfolio_path=paper_path,
        research_portfolio_path=tmp_path / "data" / "research_portfolio.json",
        research_scorecard_path=scorecard_path,
        research_thresholds_path=tmp_path / "data" / "metrics" / "research_thresholds.json",
        recommended_threshold_path=tmp_path / "data" / "metrics" / "recommended_threshold.json",
        train_status_path=tmp_path / "data" / "metrics" / "train_status.json",
        dataset_quality_path=tmp_path / "data" / "metrics" / "dataset_quality.json",
        logs_dir=tmp_path / "logs",
        edge_report_path=edge_report_path,
        ml_report_path=ml_report_path,
    )

    assert snapshot["baseline_operational_snapshot"]["closed_trades"] == 3
    assert snapshot["baseline_operational_snapshot"]["total_pnl_usd"] == pytest.approx(2.0)
    assert snapshot["research"]["normalized_candidate_events"]["ambiguous_bought_dropped"] == 1
    assert snapshot["research"]["normalized_candidate_events"]["rows_in"] == 3
    assert snapshot["research"]["normalized_candidate_events"]["rows_out"] == 2
    assert snapshot["artifacts"]["edge_report"]["stale_vs_live_close"] is True
    assert snapshot["artifacts"]["ml_report"]["stale_vs_live_close"] is True
