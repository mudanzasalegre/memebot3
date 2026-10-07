from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.database import Base
from db.models import Position, Token, TradeEvent
from runtime import close_recovery


def _event_snapshot(*, event_type: str, now: dt.datetime, qty: int, reason: str) -> dict[str, object]:
    return {
        "event_type": event_type,
        "ts_utc": now,
        "qty": qty,
        "price_usd": 1.5,
        "notional_usd": 60.0 if event_type == "partial_fill" else None,
        "pnl_usd": 20.0,
        "pnl_pct": 50.0,
        "reason": reason,
        "price_source": "paper_fill",
        "price_confidence": "high",
    }


def test_journal_is_fsynced_and_reconstructs_status_by_recovery_id(tmp_path: Path, monkeypatch) -> None:
    outbox = tmp_path / "close_recovery.jsonl"
    fsync_calls: list[int] = []
    monkeypatch.setattr(close_recovery.os, "fsync", lambda fd: fsync_calls.append(fd))
    now = dt.datetime(2026, 7, 12, 10, 0, tzinfo=dt.timezone.utc)
    pos = SimpleNamespace(
        id=7,
        address="mint-seven",
        run_id="run-seven",
        qty=60,
        entry_qty=100,
        closed=False,
        partial_taken=True,
        partial_count=1,
        partial_ladder_state='{"taken":[25]}',
        first_partial_at=now,
        last_partial_at=now,
        last_partial_qty=40,
        last_partial_price_usd=1.5,
        realized_qty=40,
        realized_proceeds_usd=60.0,
        realized_cost_usd=40.0,
        realized_pnl_usd=20.0,
        exit_state="post_partial",
    )
    record = close_recovery.build_recovery_record(
        pos,
        event_type="partial_fill",
        reason="partial_fill",
        sell_response={"signature": "sig-seven", "venue": "paper", "qty_sold": 40},
        trade_event=_event_snapshot(event_type="partial_fill", now=now, qty=40, reason="partial_fill"),
    )

    close_recovery.append_pending(outbox, record, error=RuntimeError("commit failed"))
    assert fsync_calls
    pending = close_recovery.load_pending_records(outbox)
    assert len(pending) == 1
    assert pending[0]["position_snapshot"]["qty"] == 60
    assert pending[0]["position_snapshot"]["partial_ladder_state"] == '{"taken":[25]}'
    assert close_recovery.pending_addresses(outbox) == {"mint-seven"}

    close_recovery.append_status(outbox, record, status="resolved")
    assert close_recovery.load_pending_records(outbox) == []
    assert len(fsync_calls) == 2


def test_loader_ignores_truncated_tail_but_keeps_last_durable_pending_row(tmp_path: Path) -> None:
    outbox = tmp_path / "close_recovery.jsonl"
    now = dt.datetime(2026, 7, 12, 10, 0, tzinfo=dt.timezone.utc)
    pos = SimpleNamespace(id=8, address="mint-eight", run_id="run-eight", qty=0, closed=True)
    record = close_recovery.build_recovery_record(
        pos,
        event_type="close",
        reason="STOP_LOSS",
        sell_response={"signature": "sig-eight"},
        trade_event=_event_snapshot(event_type="close", now=now, qty=100, reason="STOP_LOSS"),
    )
    close_recovery.append_pending(outbox, record, error=RuntimeError("commit failed"))
    with outbox.open("a", encoding="utf-8") as handle:
        handle.write('{"status":"resolved"')

    pending = close_recovery.load_pending_records(outbox)
    assert [row["address"] for row in pending] == ["mint-eight"]


def test_loader_fails_closed_on_corruption_before_the_tail(tmp_path: Path) -> None:
    outbox = tmp_path / "close_recovery.jsonl"
    outbox.write_text('{"status":"pending"}\nnot-json\n{"status":"resolved"}\n', encoding="utf-8")

    with pytest.raises(close_recovery.CloseRecoveryError, match="row 2"):
        close_recovery.load_pending_records(outbox)


async def _new_session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'recovery.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_position(session_factory, *, address: str) -> int:
    async with session_factory() as session:
        token = Token(address=address, symbol="REC")
        position = Position(
            address=address,
            token_mint=address,
            symbol="REC",
            qty=100,
            entry_qty=100,
            buy_price_usd=1.0,
            entry_notional_usd=100.0,
            entry_regime="pump_early",
            run_id="recovery-run",
            dry_run=True,
        )
        session.add_all([token, position])
        await session.commit()
        return int(position.id)


@pytest.mark.asyncio
async def test_restart_replay_restores_partial_fill_and_trade_event(tmp_path: Path) -> None:
    engine, session_factory = await _new_session_factory(tmp_path)
    outbox = tmp_path / "partial-recovery.jsonl"
    position_id = await _seed_position(session_factory, address="partial-mint")
    now = dt.datetime(2026, 7, 12, 10, 0, tzinfo=dt.timezone.utc)

    async with session_factory() as session:
        position = await session.get(Position, position_id)
        assert position is not None
        position.qty = 60
        position.partial_taken = True
        position.partial_count = 2
        position.partial_ladder_state = '{"taken":[25,50]}'
        position.first_partial_at = now
        position.last_partial_at = now
        position.last_partial_qty = 40
        position.last_partial_price_usd = 1.5
        position.realized_qty = 40
        position.realized_proceeds_usd = 60.0
        position.realized_cost_usd = 40.0
        position.realized_pnl_usd = 20.0
        position.exit_state = "post_partial"
        record = close_recovery.build_recovery_record(
            position,
            event_type="partial_fill",
            reason="partial_fill",
            sell_response={"signature": "partial-sig", "venue": "paper", "qty_sold": 40},
            trade_event=_event_snapshot(
                event_type="partial_fill",
                now=now,
                qty=40,
                reason="partial_fill",
            ),
        )
        await session.rollback()
    close_recovery.append_pending(outbox, record, error=RuntimeError("sqlite locked"))

    async with session_factory() as restarted_session:
        result = await close_recovery.recover_pending(restarted_session, outbox)
    assert len(result.resolved) == 1
    assert result.failed == ()
    assert result.pending_addresses == frozenset()

    async with session_factory() as verification_session:
        restored = await verification_session.get(Position, position_id)
        assert restored is not None
        assert restored.closed is False
        assert restored.qty == 60
        assert restored.realized_qty == 40
        assert restored.realized_pnl_usd == pytest.approx(20.0)
        assert restored.partial_count == 2
        assert restored.partial_ladder_state == '{"taken":[25,50]}'
        events = (
            await verification_session.execute(
                select(TradeEvent).where(TradeEvent.position_id == position_id)
            )
        ).scalars().all()
        assert len(events) == 1
        assert events[0].event_type == "partial_fill"
        assert events[0].qty == 40
        assert record["recovery_id"] in str(events[0].raw_json)

    await engine.dispose()


@pytest.mark.asyncio
async def test_replay_is_idempotent_if_db_commit_wins_before_resolved_marker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine, session_factory = await _new_session_factory(tmp_path)
    outbox = tmp_path / "close-recovery.jsonl"
    position_id = await _seed_position(session_factory, address="close-mint")
    now = dt.datetime(2026, 7, 12, 10, 0, tzinfo=dt.timezone.utc)

    async with session_factory() as session:
        position = await session.get(Position, position_id)
        assert position is not None
        position.qty = 0
        position.closed = True
        position.closed_at = now
        position.close_price_usd = 1.5
        position.exit_tx_sig = "close-sig"
        position.exit_reason = "TAKE_PROFIT"
        position.exit_reason_full = "TAKE_PROFIT"
        position.total_pnl_usd = 50.0
        position.total_pnl_pct = 50.0
        record = close_recovery.build_recovery_record(
            position,
            event_type="close",
            reason="TAKE_PROFIT",
            sell_response={"signature": "close-sig", "venue": "paper"},
            trade_event=_event_snapshot(event_type="close", now=now, qty=100, reason="TAKE_PROFIT"),
        )
        await session.rollback()
    close_recovery.append_pending(outbox, record, error=RuntimeError("commit response lost"))

    real_append_status = close_recovery.append_status

    def crash_before_resolved(path, pending_record, *, status, error=None):
        if status == "resolved":
            raise close_recovery.CloseRecoveryError("simulated crash after db commit")
        return real_append_status(path, pending_record, status=status, error=error)

    monkeypatch.setattr(close_recovery, "append_status", crash_before_resolved)
    async with session_factory() as first_restart:
        with pytest.raises(close_recovery.CloseRecoveryError, match="after db commit"):
            await close_recovery.recover_pending(first_restart, outbox)

    monkeypatch.setattr(close_recovery, "append_status", real_append_status)
    async with session_factory() as second_restart:
        result = await close_recovery.recover_pending(second_restart, outbox)
        event_count = await second_restart.scalar(
            select(func.count(TradeEvent.id)).where(TradeEvent.position_id == position_id)
        )
    assert len(result.resolved) == 1
    assert event_count == 1
    assert close_recovery.load_pending_records(outbox) == []

    await engine.dispose()


def test_run_bot_contract_captures_before_commit_and_replays_before_tasks() -> None:
    source = Path("run_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="run_bot.py")
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    check_positions = functions["_check_positions"]
    builder_lines = [
        node.lineno
        for node in ast.walk(check_positions)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_build_close_persistence_recovery"
    ]
    recovery_commits = [
        node
        for node in ast.walk(check_positions)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_commit_close_persistence"
    ]
    assert len(builder_lines) == len(recovery_commits) == 4
    for call in recovery_commits:
        assert isinstance(call.args[1], ast.Name)
        assert call.args[1].id in {"close_recovery_record", "partial_recovery_record"}
        assert any(builder_line < call.lineno for builder_line in builder_lines)

    commit_helper = functions["_commit_close_persistence"]

    def helper_call_line(name: str) -> int:
        return min(
            node.lineno
            for node in ast.walk(commit_helper)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        )

    arm_line = helper_call_line("_arm_close_persistence_recovery")
    rollback_line = min(
        node.lineno
        for node in ast.walk(commit_helper)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "rollback"
    )
    db_commit_line = min(
        node.lineno
        for node in ast.walk(commit_helper)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
    )
    record_line = helper_call_line("_record_close_persistence_recovery")
    resolve_line = helper_call_line("_resolve_close_persistence_recovery")
    assert arm_line < db_commit_line < resolve_line
    assert rollback_line < record_line

    retry_lines = [
        node.lineno
        for node in ast.walk(check_positions)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_recover_close_persistence_outbox"
    ]
    load_lines = [
        node.lineno
        for node in ast.walk(check_positions)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_load_open_positions"
    ]
    assert retry_lines and load_lines and min(retry_lines) < min(load_lines)

    runner = functions["_runner"]
    startup_retry = min(
        node.lineno
        for node in ast.walk(runner)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_recover_close_persistence_outbox"
    )
    gather = min(
        node.lineno
        for node in ast.walk(runner)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "gather"
    )
    assert startup_retry < gather
    assert "close_recovery_pending" in source
