from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from analytics.current_run_summary import build_current_run_summary
from analytics.paper_real_outcomes import build_paper_real_outcomes_report
from analytics.report_utils import load_sqlite_closed_trades, load_sqlite_positions, position_key
from db.database import add_trade_event, set_position_exit_reason
from db.models import Position


def _write_runtime_identity(root: Path, run_id: str = "run-pr01") -> None:
    metrics = root / "data" / "metrics"
    metrics.mkdir(parents=True, exist_ok=True)
    (metrics / "runtime_events.jsonl").write_text(
        json.dumps(
            {
                "event_type": "actual_paper_buy",
                "address": "MINT",
                "run_id": run_id,
                "run_started_at": "2026-07-07T10:00:00+00:00",
                "ts_utc": "2026-07-07T10:01:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (metrics / "candidate_outcomes.jsonl").write_text("", encoding="utf-8")


def _write_reentry_db(root: Path) -> None:
    db_path = root / "data" / "memebotdatabase.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY,
                address TEXT,
                token_mint TEXT,
                run_id TEXT,
                opened_at TEXT,
                closed_at TEXT,
                closed INTEGER,
                exit_reason TEXT,
                exit_reason_full TEXT,
                source_position_key TEXT,
                total_pnl_pct REAL,
                total_pnl_usd REAL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO positions (
                id, address, token_mint, run_id, opened_at, closed_at, closed,
                exit_reason, exit_reason_full, source_position_key, total_pnl_pct, total_pnl_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "MINT",
                    "MINT",
                    "run-pr01",
                    "2026-07-07T10:01:00+00:00",
                    "2026-07-07T10:05:00+00:00",
                    1,
                    "POST_PARTIAL_TRAILING_E",
                    "POST_PARTIAL_TRAILING_EXTENDED_REASON",
                    "run-pr01:1",
                    50.0,
                    1.5,
                ),
                (
                    2,
                    "MINT",
                    "MINT",
                    "run-pr01",
                    "2026-07-07T10:10:00+00:00",
                    "2026-07-07T10:15:00+00:00",
                    1,
                    "TOTAL_PNL_PROTECTION_",
                    "TOTAL_PNL_PROTECTION_EXIT_AFTER_PARTIAL",
                    "run-pr01:2",
                    -20.0,
                    -0.5,
                ),
            ],
        )
        conn.execute(
            """
            CREATE VIEW closed_trade_view AS
            SELECT
                id, address, token_mint, run_id, opened_at, closed_at, closed,
                COALESCE(exit_reason_full, exit_reason) AS exit_reason,
                exit_reason_full,
                source_position_key,
                total_pnl_pct,
                total_pnl_usd
            FROM positions
            WHERE closed = 1
            """
        )
        conn.commit()


def test_sqlite_loader_preserves_reentry_rows_and_full_exit_reasons(tmp_path: Path) -> None:
    _write_reentry_db(tmp_path)

    rows = load_sqlite_positions(tmp_path)
    closed_rows = load_sqlite_closed_trades(tmp_path)

    assert len(rows) == 2
    assert len({position_key(row) for row in rows}) == 2
    assert [row["exit_reason"] for row in closed_rows] == [
        "POST_PARTIAL_TRAILING_EXTENDED_REASON",
        "TOTAL_PNL_PROTECTION_EXIT_AFTER_PARTIAL",
    ]


def test_reports_use_sqlite_closed_trades_as_canonical_over_paper_overwrite(tmp_path: Path) -> None:
    _write_runtime_identity(tmp_path)
    _write_reentry_db(tmp_path)
    (tmp_path / "data" / "paper_portfolio.json").write_text(
        json.dumps(
            {
                "positions": [
                    {
                        "address": "MINT",
                        "run_id": "run-pr01",
                        "opened_at": "2026-07-07T10:10:00+00:00",
                        "closed_at": "2026-07-07T10:15:00+00:00",
                        "closed": True,
                        "total_pnl_usd": -0.5,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    outcomes = build_paper_real_outcomes_report(tmp_path)
    summary = build_current_run_summary(tmp_path)

    assert outcomes["summary"]["closed"] == 2
    assert outcomes["summary"]["total_pnl_usd"] == 1.0
    assert outcomes["sources"]["paper_portfolio"] == 1
    assert outcomes["sources"]["sqlite_positions"] == 2
    assert outcomes["sources"]["deduped_rows"] == 2
    assert summary["closed_positions"] == 2
    assert summary["closed_trades"] == 2
    assert summary["total_pnl_usd"] == 1.0


def test_position_exit_reason_keeps_full_reason_and_legacy_alias() -> None:
    reason = "TOTAL_PNL_PROTECTION_EXIT_AFTER_PARTIAL"
    pos = Position(id=7, address="MINT", qty=100, buy_price_usd=1.0)

    canonical = set_position_exit_reason(pos, reason)

    assert canonical == reason
    assert pos.exit_reason_full == reason
    assert pos.exit_reason == reason[:24]


def test_add_trade_event_sets_source_key_and_payload() -> None:
    class DummySession:
        def __init__(self) -> None:
            self.added = []

        def add(self, event) -> None:
            self.added.append(event)

    session = DummySession()
    pos = Position(id=42, address="MINT", token_mint="MINT", qty=50, buy_price_usd=1.0)

    event = add_trade_event(
        session,  # type: ignore[arg-type]
        pos,
        event_type="partial_fill",
        qty=25,
        price_usd=1.5,
        notional_usd=37.5,
        pnl_usd=12.5,
        pnl_pct=50.0,
        reason="partial_fill",
        price_source="jupiter",
        price_confidence="high",
    )

    assert pos.source_position_key == "42"
    assert session.added == [event]
    assert event.event_type == "partial_fill"
    assert event.position_id == 42
    assert event.reason == "partial_fill"
    assert event.price_confidence == "high"
