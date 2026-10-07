"""Synthetic fills and isolated files/SQLite only; never starts the bot."""
from __future__ import annotations

import ast
import asyncio
import copy
import datetime as dt
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analytics import runner_ladder
from db.database import Base, add_trade_event, set_position_exit_reason
from db.models import Position, Token, TradeEvent
from runtime import close_recovery as close
from runtime.buy_recovery import position_from_snapshot
from runtime.sell_recovery import SellOutcomeUncertain, SellRecoveryError, SellRecoveryStore
from trade_pnl import apply_partial_fill, summarize_trade
from trader import papertrading as paper
from utils.time import parse_iso_utc

MINT = "A" * 32
STAMP = dt.datetime(2026, 10, 7, 12, tzinfo=dt.timezone.utc)


def position(*, paper_mode=True):
    return Position(id=1, address=MINT, token_mint=MINT, symbol="SYNTHETIC", qty=1000,
        entry_qty=1000, buy_price_usd=1., entry_notional_usd=10., buy_amount_sol=.1,
        dry_run=paper_mode, opened_at=STAMP, run_id="synthetic-run", entry_regime="pump_early",
        source_position_key="buy:original-entry", realized_qty=0, realized_proceeds_usd=0.,
        realized_cost_usd=0., realized_pnl_usd=0., partial_count=0, closed=False)


def entry():
    return paper._ensure_entry_accounting({"qty_lamports": 1000, "entry_qty": 1000,
        "buy_price_usd": 1., "entry_notional_usd": 10., "amount_sol": .1,
        "closed": False, "opened_at": STAMP.isoformat(), "run_id": "synthetic-run",
        "entry_intent_id": "original-entry", "buy_signature": "SIM-original-entry"})


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(paper, "_PORTFOLIO", {MINT: entry()})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "data" / "paper_portfolio.json")
    monkeypatch.setattr(paper, "_resolve_close_price_usd", AsyncMock(return_value=(2., "synthetic")))
    monkeypatch.setattr(paper, "get_sol_usd", AsyncMock(return_value=100.))
    monkeypatch.setattr(paper.runner_forward, "register_partial", lambda *a, **k: None)
    paper._save(strict=True)
    return tmp_path


def namespace(root, store, *, seller=paper, paper_mode=True):
    """Execute the actual guarded/SQL helpers without run_bot import side effects."""
    names = {"_sell_position_guarded", "_build_sell_recovery_from_intent", "_build_close_persistence_recovery",
        "_record_partial_trade_fill", "_seal_closed_trade_metrics", "_finalize_position_runner_metrics",
        "_arm_close_persistence_recovery", "_resolve_close_persistence_recovery", "_commit_close_persistence",
        "_record_close_persistence_recovery", "_recover_close_persistence_outbox"}
    names.add("_seconds_from_opened_at")
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert len(nodes) == len(names)
    ns = {"Position": Position, "Optional": Optional, "dt": dt, "time": time,
        "SessionLocal": object, "DRY_RUN": paper_mode, "seller": seller, "_SELL_RECOVERY": store,
        "position_from_snapshot": position_from_snapshot, "parse_iso_utc": parse_iso_utc,
        "set_position_exit_reason": set_position_exit_reason, "runner_ladder": runner_ladder,
        "apply_partial_fill": apply_partial_fill, "summarize_trade": summarize_trade,
        "build_recovery_record": close.build_recovery_record, "CloseRecoveryError": close.CloseRecoveryError,
        "prepare_close_recovery": close.append_prepared, "append_close_recovery_status": close.append_status,
        "append_close_recovery_pending": close.append_pending,
        "load_close_recovery_pending_addresses": close.pending_addresses,
        "replay_close_recovery_pending": close.recover_pending,
        "_CLOSE_RECOVERY_OUTBOX_PATH": root / "close.jsonl", "_CLOSE_RECOVERY_PENDING": set(),
        "_last_close_recovery_retry_monotonic": 0., "_CLOSE_RECOVERY_RETRY_INTERVAL_S": 0.,
        "_activate_close_recovery_pause": lambda: None, "_release_close_recovery_pause_if_safe": lambda: None,
        "errors": [], "record_runtime_event": lambda *a, **k: None,
        "_bootstrap_strategy_runtime": AsyncMock(),
        "log": SimpleNamespace(warning=lambda *a, **k: None, critical=lambda *a, **k: None,
            error=lambda *a, **k: None, debug=lambda *a, **k: None)}
    ns["_note_runtime_error"] = lambda *a: ns["errors"].append(a)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "run_bot.py", "exec"), ns)
    return ns


async def database(root):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ledger.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add_all([Token(address=MINT), position()])
        await session.commit()
    return engine, sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [400, 1000])
async def test_paper_sell_write_failure_preserves_money_and_original_references(isolated, monkeypatch, quantity):
    original = paper._PORTFOLIO[MINT]
    before, contents = copy.deepcopy(original), paper._DATA_PATH.read_bytes()
    def fail(*a): raise OSError("synthetic disk error")
    monkeypatch.setattr(paper, "write_json_atomic", fail)
    with pytest.raises(paper.PaperPortfolioError): await paper.sell(MINT, quantity)
    assert paper._PORTFOLIO[MINT] is original and original == before
    assert paper._DATA_PATH.read_bytes() == contents
    assert not (isolated / "data" / "paper_closed_trades.jsonl").exists()
    assert not paper._SELL_LOCKS


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [True, 1.5, float("nan"), 0, -1, 1001, 2**63])
async def test_invalid_paper_quantity_never_creates_fill(isolated, quantity):
    response = await paper.sell(MINT, quantity)
    assert response["ok"] is False and response["error"] == "INVALID_QUANTITY"
    assert paper._PORTFOLIO[MINT]["qty_lamports"] == 1000
    paper._resolve_close_price_usd.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_partials_are_serial_and_same_intent_is_idempotent(isolated, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def price(**kwargs):
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return 2., "synthetic"
    monkeypatch.setattr(paper, "_resolve_close_price_usd", price)
    first = asyncio.create_task(paper.sell(MINT, 400, exit_intent_id="a" * 32))
    await entered.wait()
    second = asyncio.create_task(paper.sell(MINT, 600, exit_intent_id="b" * 32))
    await asyncio.sleep(0)
    assert len(calls) == 1
    release.set()
    responses = await asyncio.gather(first, second)
    assert responses[0]["qty_left"] == 600 and responses[1]["qty_left"] == 0
    assert paper._PORTFOLIO[MINT]["closed"] and len(paper._PORTFOLIO[MINT]["exit_fill_events"]) == 2
    assert await paper.sell(MINT, 600, exit_intent_id="b" * 32) == responses[1]
    assert not paper._SELL_LOCKS and len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [400, 1000])
@pytest.mark.parametrize("stage", ["prepared", "fill_received", "sql_prepared"])
async def test_paper_restart_recovers_once_at_every_sell_boundary(isolated, quantity, stage):
    engine, sessions = await database(isolated)
    store = SellRecoveryStore(isolated / "intents")
    ns = namespace(isolated, store)
    async with sessions() as session:
        pos = await session.get(Position, 1)
    plan = {"pending_step_count": 2, "next_state": runner_ladder.initial_ladder_state()}
    attempt = store.begin(pos, quantity, paper=True, reason="SYNTHETIC_EXIT", partial_plan=plan, paper_before=entry())
    response = await paper.sell(MINT, quantity, exit_intent_id=attempt.intent_id, partial_ladder_plan=plan)
    if stage != "prepared":
        checked = attempt.receive(response)
        if stage == "sql_prepared": ns["_build_sell_recovery_from_intent"](attempt.row, checked)
    assert not store.pending_addresses  # Active submission is never replayed.
    records, failures = store.recover_to_outbox(paper_portfolio=paper.load_portfolio(), build_record=ns["_build_sell_recovery_from_intent"])
    assert not records and not failures
    store.finish(attempt)
    restarted = SellRecoveryStore(store.directory)
    ns = namespace(isolated, restarted)
    try:
        async with sessions() as session:
            assert await ns["_recover_close_persistence_outbox"](session, force=True) == 1, ns["errors"]
            assert await ns["_recover_close_persistence_outbox"](session, force=True) == 0
        async with sessions() as session:
            restored = await session.get(Position, 1)
            assert restored.qty == 1000 - quantity and restored.closed is (quantity == 1000)
            if quantity == 400: assert restored.partial_count == 2 and restored.realized_pnl_usd == pytest.approx(4.)
            events = (await session.execute(select(TradeEvent))).scalars().all()
            assert len(events) == 1 and events[0].qty == quantity
        assert not restarted.pending_addresses and not ns["_CLOSE_RECOVERY_PENDING"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "response_lost", "before_sql", "sql_commit", "sql_ack"])
async def test_actual_guard_and_sql_helpers_recover_without_reselling(isolated, monkeypatch, failure):
    store = SellRecoveryStore(isolated / "intents")
    calls = []
    async def execute(*a, **k):
        calls.append(True)
        result = await paper.sell(*a, **k)
        if failure == "response_lost": raise asyncio.CancelledError()
        return result
    ns = namespace(isolated, store, seller=SimpleNamespace(sell=execute))
    engine, sessions = await database(isolated)
    try:
        async with sessions() as session:
            pos = await session.get(Position, 1)
            if failure == "response_lost":
                with pytest.raises(asyncio.CancelledError): await ns["_sell_position_guarded"](pos, 400, reason="partial_fill")
            else:
                response = await ns["_sell_position_guarded"](pos, 400, reason="partial_fill")
                record = store.records[response["_sell_intent_id"]]["sql_record"]
                if failure != "before_sql":
                    close.apply_position_snapshot(pos, record["position_snapshot"])
                    event = record["trade_event"]
                    add_trade_event(session, pos, **{**event, "ts_utc": parse_iso_utc(event["ts_utc"])})
                    if failure == "sql_commit":
                        monkeypatch.setattr(session, "commit", AsyncMock(side_effect=RuntimeError("synthetic commit")))
                    if failure == "sql_ack":
                        def fail(*a): raise SellRecoveryError("synthetic ack failure")
                        monkeypatch.setattr(store, "acknowledge", fail)
                        with pytest.raises(SellRecoveryError): await ns["_commit_close_persistence"](session, record)
                    else:
                        assert await ns["_commit_close_persistence"](session, record) is (failure == "none")
        restarted = SellRecoveryStore(store.directory)
        ns = namespace(isolated, restarted)
        async with sessions() as session:
            await ns["_recover_close_persistence_outbox"](session, force=True)
        async with sessions() as session:
            pos = await session.get(Position, 1)
            assert pos.qty == 600 and pos.realized_pnl_usd == pytest.approx(4.)
            assert len((await session.execute(select(TradeEvent))).scalars().all()) == 1
        assert len(calls) == 1 and not restarted.pending_addresses
    finally:
        await engine.dispose()


def test_missing_journal_is_read_only_and_prepare_failure_blocks_submission(isolated, monkeypatch):
    import runtime.sell_recovery as recovery
    store = SellRecoveryStore(isolated / "intents")
    assert not store.directory.exists()
    def fail(*a): raise OSError("synthetic disk")
    monkeypatch.setattr(recovery, "write_json_atomic", fail)
    with pytest.raises(SellRecoveryError): store.begin(position(), 400, paper=True, reason="partial", paper_before=entry())
    assert not store.records and not store.active


@pytest.mark.parametrize("paper_mode", [True, False])
def test_absent_paper_fill_can_resolve_but_live_absence_remains_uncertain(isolated, paper_mode):
    store = SellRecoveryStore(isolated / "intents")
    attempt = store.begin(position(paper_mode=paper_mode), 400, paper=paper_mode, reason="partial",
        paper_before=entry() if paper_mode else None)
    store.finish(attempt)
    records, failures = store.recover_to_outbox(paper_portfolio={MINT: entry()}, build_record=lambda *a: None)
    assert not records
    assert bool(failures) is not paper_mode
    assert bool(store.pending_addresses) is not paper_mode


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, {}, {"ok": False, "signature": "ERROR"},
    {"ok": True, "signature": "submitted", "price_used_usd": 2.}])
async def test_uncertain_sell_response_is_quarantined_and_cannot_resubmit(isolated, response):
    store = SellRecoveryStore(isolated / "intents")
    seller = SimpleNamespace(sell=AsyncMock(return_value=response))
    ns = namespace(isolated, store, seller=seller)
    with pytest.raises(SellOutcomeUncertain): await ns["_sell_position_guarded"](position(), 400, reason="partial")
    assert ns["_CLOSE_RECOVERY_PENDING"] == {MINT} and store.pending_addresses == {MINT}
    with pytest.raises(SellRecoveryError): await ns["_sell_position_guarded"](position(), 400, reason="partial")
    assert seller.sell.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [True, False])
@pytest.mark.parametrize("error", [TimeoutError, TypeError])
async def test_live_jupiter_submission_never_retries_or_falls_back(monkeypatch, managed, error):
    from trader import seller
    calls = []
    async def execute(quote=None, **kwargs):
        calls.append(True)
        raise error("synthetic ambiguous submission")
    router = SimpleNamespace(JUP_API_KEY="synthetic" if managed else "", execute_managed_swap=execute,
        execute_swap=execute, get_quote=AsyncMock(return_value=SimpleNamespace(ok=True, raw={},
            price_impact_bps=1, in_amount=400, out_amount=1)))
    fallback = AsyncMock()
    monkeypatch.setattr(seller, "jupiter", router)
    monkeypatch.setattr(seller, "_JUP_ROUTER_AVAILABLE", True)
    monkeypatch.setattr(seller.gmgn, "sell", fallback)
    with pytest.raises(SellOutcomeUncertain):
        await seller.sell(MINT, 400, token_mint=MINT, liquidity_usd=100000.)
    assert len(calls) == 1
    fallback.assert_not_called()


@pytest.mark.asyncio
async def test_live_gmgn_uncertain_result_cannot_become_known_no_fill(monkeypatch):
    from trader import seller
    monkeypatch.setattr(seller, "_JUP_ROUTER_AVAILABLE", False)
    execution = AsyncMock(side_effect=TimeoutError("synthetic submission"))
    monkeypatch.setattr(seller.gmgn, "sell", execution)
    with pytest.raises(SellOutcomeUncertain): await seller.sell(MINT, 400, liquidity_usd=100000.)
    assert execution.await_count == 1


@pytest.mark.asyncio
async def test_old_partial_ack_replay_cannot_reopen_a_later_close(isolated):
    engine, sessions = await database(isolated)
    store = SellRecoveryStore(isolated / "intents")
    ns = namespace(isolated, store)
    async with sessions() as session:
        pos = await session.get(Position, 1)
        response = await ns["_sell_position_guarded"](pos, 400, reason="partial_fill")
    record = store.records[response["_sell_intent_id"]]["sql_record"]
    outbox = isolated / "partial.jsonl"
    close.append_prepared(outbox, record)
    try:
        async with sessions() as session:
            assert len((await close.recover_pending(session, outbox)).resolved) == 1
            pos = await session.get(Position, 1)
            pos.qty, pos.closed, pos.total_pnl_usd = 0, True, 123.
            await session.commit()
        close.append_prepared(outbox, record)  # Simulate a lost acknowledgement.
        async with sessions() as session:
            assert len((await close.recover_pending(session, outbox)).resolved) == 1
            pos = await session.get(Position, 1)
            assert pos.closed and pos.qty == 0 and pos.total_pnl_usd == 123.
    finally:
        await engine.dispose()


def test_all_primary_exit_routes_use_pre_execution_guard():
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    monitor = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_check_positions")
    calls = [node for node in ast.walk(monitor) if isinstance(node, ast.Call)]
    guarded = [node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "_sell_position_guarded"]
    assert len(guarded) == 3
    assert all(any(key.arg == "reason" for key in node.keywords) for node in guarded)
    assert not any(isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "seller" and node.func.attr == "sell" for node in calls)


@pytest.mark.asyncio
async def test_known_pre_execution_rejection_does_not_quarantine_or_fake_a_fill(isolated):
    store = SellRecoveryStore(isolated / "intents")
    ns = namespace(isolated, store, seller=SimpleNamespace(sell=AsyncMock(
        return_value={"ok": False, "error": "EXIT_QUOTE_UNAVAILABLE", "qty_sold": 0})))
    result = await ns["_sell_position_guarded"](position(), 400, reason="partial")
    assert result["ok"] is False and not store.pending_addresses and not ns["_CLOSE_RECOVERY_PENDING"]
    assert not paper._PORTFOLIO[MINT].get("exit_fill_events")


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("qty_sold", True), ("qty_sold", 399), ("qty_sold", 2**63),
    ("qty_left", 599), ("partial", False), ("signature", ""), ("price_used_usd", float("nan")),
    ("price_used_usd", float("inf")), ("price_used_usd", 0.)])
async def test_invalid_positive_receipts_remain_pending(isolated, field, value):
    store = SellRecoveryStore(isolated / "intents")
    attempt = store.begin(position(), 400, paper=True, reason="partial", paper_before=entry())
    response = await paper.sell(MINT, 400, exit_intent_id=attempt.intent_id)
    response[field] = value
    with pytest.raises(SellOutcomeUncertain): attempt.receive(response)
    store.finish(attempt)
    assert store.pending_addresses == {MINT} and store.records[attempt.intent_id]["state"] == "prepared"


@pytest.mark.asyncio
async def test_receipt_and_sql_states_cannot_be_rewritten_by_conflicting_responses(isolated):
    engine, sessions = await database(isolated)
    store = SellRecoveryStore(isolated / "intents")
    ns = namespace(isolated, store)
    try:
        async with sessions() as session:
            pos = await session.get(Position, 1)
            attempt = store.begin(pos, 400, paper=True, reason="partial", paper_before=entry())
            response = await paper.sell(MINT, 400, exit_intent_id=attempt.intent_id)
            checked = attempt.receive(response)
            with pytest.raises(SellOutcomeUncertain): attempt.receive({**response, "price_used_usd": 3.})
            with pytest.raises(SellOutcomeUncertain): attempt.receive({"ok": False, "error": "NO_QTY"})
            ns["_build_sell_recovery_from_intent"](attempt.row, checked)
            # The caller's attempt object still has its earlier in-memory state.
            # The authoritative store must reject its stale write nevertheless.
            with pytest.raises(SellRecoveryError): attempt.receive(response)
            record = copy.deepcopy(store.records[attempt.intent_id]["sql_record"])
            record["trade_event"]["price_usd"] = 99.
            with pytest.raises(SellRecoveryError): store.prepare_sql(record)
            with pytest.raises(SellRecoveryError): store.acknowledge(record)
            store.acknowledge(store.records[attempt.intent_id]["sql_record"])
            with pytest.raises(SellRecoveryError): attempt.receive(response)
            assert not store.records
    finally:
        store.finish(attempt)
        await engine.dispose()


@pytest.mark.parametrize("field", ["qty_lamports", "entry_qty", "buy_price_usd", "entry_notional_usd", "entry_intent_id"])
def test_paper_sql_entry_conflicts_block_before_submission(isolated, field):
    store = SellRecoveryStore(isolated / "intents")
    before = entry()
    before[field] = "conflicting"
    with pytest.raises(SellRecoveryError): store.begin(position(), 400, paper=True, reason="partial", paper_before=before)
    assert not store.directory.exists()


def test_corrupt_sell_journal_is_preserved_and_never_treated_as_empty(isolated):
    store = SellRecoveryStore(isolated / "intents")
    attempt = store.begin(position(), 400, paper=True, reason="partial", paper_before=entry())
    path = store.directory / (attempt.intent_id + ".json")
    path.write_text('{"broken":', encoding="utf-8")
    with pytest.raises(SellRecoveryError): SellRecoveryStore(store.directory)
    assert path.read_text(encoding="utf-8") == '{"broken":'


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["address", "run_id", "source_position_key", "expected_before_qty"])
async def test_sql_recovery_ownership_or_quantity_conflict_never_mutates_the_position(isolated, conflict):
    engine, sessions = await database(isolated)
    store = SellRecoveryStore(isolated / "intents")
    ns = namespace(isolated, store)
    try:
        async with sessions() as session:
            pos = await session.get(Position, 1)
            response = await ns["_sell_position_guarded"](pos, 400, reason="partial")
        record = copy.deepcopy(store.records[response["_sell_intent_id"]]["sql_record"])
        record[conflict] = 999 if conflict == "expected_before_qty" else "other-owner"
        outbox = isolated / "conflict.jsonl"
        close.append_prepared(outbox, record)
        async with sessions() as session:
            result = await close.recover_pending(session, outbox)
            assert len(result.failed) == 1 and not result.resolved
            pos = await session.get(Position, 1)
            assert pos.qty == 1000 and not pos.closed
            assert not (await session.execute(select(TradeEvent))).scalars().all()
    finally:
        await engine.dispose()
