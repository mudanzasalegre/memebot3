"""Synthetic orders, isolated files/SQLite and mocked submission; no bot run."""
from __future__ import annotations

import ast
import asyncio
import datetime as dt
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.database import Base
from db.models import Position, Token
from runtime.buy_recovery import BuyOutcomeUncertain, BuyRecoveryError, BuyRecoveryStore
from utils import atomic_json

MINT = "A" * 32
STAMP = dt.datetime(2026, 10, 7, 12, tzinfo=dt.timezone.utc)


def prototype(*, paper=True):
    return Position(address=MINT, token_mint=MINT, symbol="ORIGINAL", qty=0, entry_qty=0,
        buy_price_usd=0., buy_amount_sol=.1, dry_run=paper, opened_at=STAMP,
        entry_notional_usd=0., entry_lane="runner", entry_ai_proba=.6,
        runner_exit_profile="runner-frozen", run_id="synthetic-run")


def fill(intent):
    return {"qty_lamports": 1000, "signature": "SIM-" + intent.intent_id,
        "buy_price_usd": 1., "entry_notional_usd": 10., "price_source": "synthetic",
        "price_confidence": "unknown", "runner_trailing_policy": None}


def portfolio(intent, *, closed=False, qty=1000):
    return {MINT: {"entry_intent_id": intent.intent_id, "buy_signature": "SIM-" + intent.intent_id,
        "entry_qty": 1000, "qty_lamports": qty, "amount_sol": .1, "buy_price_usd": 1.,
        "entry_notional_usd": 10., "price_source": "synthetic", "price_confidence": "unknown",
        "opened_at": STAMP.isoformat(), "closed": closed, "runner_trailing_policy": None}}


def filled_position(intent, *, paper=True):
    pos = prototype(paper=paper)
    pos.qty = pos.entry_qty = 1000
    pos.buy_price_usd, pos.entry_notional_usd = 1., 10.
    pos.buy_tx_sig = "SIM-" + intent.intent_id
    return pos


async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'recovery.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_atomic_failure_preserves_previous_complete_document_and_cleans_only_own_temp(tmp_path, monkeypatch):
    path = tmp_path / "portfolio.json"
    atomic_json.write_json_atomic(path, {"old": 1})
    previous = path.read_bytes()
    def fail(*args): raise OSError("synthetic replace failure")
    monkeypatch.setattr(atomic_json.os, "replace", fail)
    with pytest.raises(OSError): atomic_json.write_json_atomic(path, {"new": 2})
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_atomic_writer_rejects_nonfinite_before_touching_destination(tmp_path, value):
    path = tmp_path / "state.json"
    with pytest.raises(ValueError): atomic_json.write_json_atomic(path, {"number": value})
    assert not path.exists() and not list(tmp_path.iterdir())


def test_journal_prepare_failure_prevents_any_submission(tmp_path, monkeypatch):
    import runtime.buy_recovery as recovery
    store, submitted = BuyRecoveryStore(tmp_path / "journal"), []
    def fail(*args): raise OSError("synthetic disk failure")
    monkeypatch.setattr(recovery, "write_json_atomic", fail)
    with store.scope(), pytest.raises(BuyRecoveryError):
        store.begin(prototype(), paper=True, amount_sol=.1)
        submitted.append(True)
    assert not submitted and not store._active


def test_missing_first_launch_journal_is_read_only_and_requires_owned_scope(tmp_path):
    directory = tmp_path / "journal"
    store = BuyRecoveryStore(directory)
    assert not directory.exists() and not store.pending_addresses
    with pytest.raises(BuyRecoveryError, match="owned"):
        store.begin(prototype(), paper=True, amount_sol=.1)


@pytest.mark.asyncio
async def test_wait_for_child_ownership_survives_timeout_and_blocks_next_buy(tmp_path):
    store = BuyRecoveryStore(tmp_path / "journal")
    async def child():
        store.begin(prototype(), paper=True, amount_sol=.1)
        assert not store.pending_addresses
        await asyncio.Event().wait()
    with store.scope():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(child(), timeout=.01)
    assert store.pending_addresses == {MINT} and not store._active
    with store.scope(), pytest.raises(BuyRecoveryError, match="unresolved"):
        store.begin(prototype(), paper=True, amount_sol=.1)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepared", "fill_received", "position_prepared"])
async def test_paper_restart_reconstructs_entry_once_at_every_durable_stage(tmp_path, stage):
    engine, sessions = await database(tmp_path)
    store = BuyRecoveryStore(tmp_path / "journal")
    with store.scope():
        intent = store.begin(prototype(), paper=True, amount_sol=.1)
        if stage != "prepared": intent.receive(fill(intent))
        if stage == "position_prepared": intent.capture_position(filled_position(intent))
    restarted = BuyRecoveryStore(store.directory)
    try:
        async with sessions() as session:
            result = await restarted.recover(session, paper_portfolio=portfolio(intent))
            assert result["resolved"] == [intent.intent_id] and not result["failed"]
            rows = (await session.execute(select(Position))).scalars().all()
            assert len(rows) == 1 and rows[0].qty == 1000
            assert rows[0].source_position_key == "buy:" + intent.intent_id
            assert rows[0].symbol == "ORIGINAL" and rows[0].entry_ai_proba == .6
            assert rows[0].buy_amount_sol == .1 and rows[0].runner_exit_profile == "runner-frozen"
            assert (await restarted.recover(session, paper_portfolio=portfolio(intent)))["resolved"] == []
            assert len((await session.execute(select(Position))).scalars().all()) == 1
        assert not BuyRecoveryStore(store.directory).pending_addresses
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_prepared_paper_without_fill_resolves_but_live_unknown_never_does(tmp_path):
    engine, sessions = await database(tmp_path)
    try:
        for paper in (True, False):
            store = BuyRecoveryStore(tmp_path / str(paper))
            with store.scope(): intent = store.begin(prototype(paper=paper), paper=paper, amount_sol=.1)
            async with sessions() as session:
                result = await store.recover(session, paper_portfolio={})
                assert bool(result["resolved"]) is paper
                assert bool(result["pending_addresses"]) is not paper
                assert not (await session.execute(select(Position))).scalars().all()
    finally: await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_cannot_touch_an_active_call_or_commit(tmp_path):
    engine, sessions = await database(tmp_path)
    store = BuyRecoveryStore(tmp_path / "journal")
    try:
        with store.scope():
            intent = store.begin(prototype(), paper=True, amount_sol=.1)
            intent.receive(fill(intent))
            async with sessions() as session:
                assert (await store.recover(session, paper_portfolio=portfolio(intent)))["resolved"] == []
                assert not (await session.execute(select(Position))).scalars().all()
        assert store.pending_addresses == {MINT}
    finally: await engine.dispose()


@pytest.mark.asyncio
async def test_sql_commit_without_journal_ack_never_reopens_later_closed_position(tmp_path):
    engine, sessions = await database(tmp_path)
    store = BuyRecoveryStore(tmp_path / "journal")
    try:
        with store.scope():
            intent = store.begin(prototype(), paper=True, amount_sol=.1)
            intent.receive(fill(intent))
            pos = filled_position(intent)
            intent.capture_position(pos)
            async with sessions() as session:
                session.add_all([Token(address=MINT), pos])
                await session.commit()
                pos.closed, pos.qty, pos.realized_qty = True, 0, 1000
                pos.realized_pnl_usd, pos.exit_reason = 10., "SYNTHETIC_CLOSE"
                await session.commit()
        restarted = BuyRecoveryStore(store.directory)
        async with sessions() as session:
            result = await restarted.recover(session, paper_portfolio=portfolio(intent, closed=True, qty=0))
            assert result["resolved"] == [intent.intent_id] and not result["failed"]
            actual = (await session.execute(select(Position))).scalar_one()
            assert actual.closed and actual.qty == 0 and actual.realized_qty == 1000
            assert actual.realized_pnl_usd == 10. and actual.exit_reason == "SYNTHETIC_CLOSE"
    finally: await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing_store", "wrong_identity", "changed_quantity", "wrong_price"])
async def test_conflicting_paper_evidence_stays_quarantined_without_inserting_position(tmp_path, mutation):
    engine, sessions = await database(tmp_path)
    store = BuyRecoveryStore(tmp_path / "journal")
    with store.scope():
        intent = store.begin(prototype(), paper=True, amount_sol=.1)
        intent.receive(fill(intent))
    entries = portfolio(intent)
    if mutation == "missing_store": entries = {}
    elif mutation == "wrong_identity": entries[MINT]["entry_intent_id"] = "b" * 32
    elif mutation == "changed_quantity": entries[MINT]["qty_lamports"] = 900
    else: entries[MINT]["buy_price_usd"] = 99.
    try:
        async with sessions() as session:
            result = await store.recover(session, paper_portfolio=entries)
            assert result["failed"] and store.pending_addresses == {MINT}
            assert not (await session.execute(select(Position))).scalars().all()
    finally: await engine.dispose()


def test_corrupt_journal_is_preserved_and_fails_closed(tmp_path):
    path = tmp_path / ("a" * 32 + ".json")
    path.write_text('{"incomplete":', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(BuyRecoveryError): BuyRecoveryStore(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepared", "fill_received", "position_prepared"])
async def test_live_restart_never_synthesizes_a_wallet_confirmed_position(tmp_path, stage):
    engine, sessions = await database(tmp_path)
    store = BuyRecoveryStore(tmp_path / "journal")
    with store.scope():
        intent = store.begin(prototype(paper=False), paper=False, amount_sol=.1)
        if stage != "prepared": intent.receive(fill(intent))
        if stage == "position_prepared": intent.capture_position(filled_position(intent, paper=False))
    try:
        async with sessions() as session:
            result = await store.recover(session, paper_portfolio=portfolio(intent))
            assert not result["resolved"] and result["failed"]
            assert store.pending_addresses == {MINT}
            assert not (await session.execute(select(Position))).scalars().all()
    finally: await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_commit_failure_retains_snapshot_and_can_be_retried_without_submission(tmp_path, monkeypatch):
    engine, sessions = await database(tmp_path)
    store = BuyRecoveryStore(tmp_path / "journal")
    with store.scope():
        intent = store.begin(prototype(), paper=True, amount_sol=.1)
        intent.receive(fill(intent))
        intent.capture_position(filled_position(intent))
    try:
        async with sessions() as session:
            monkeypatch.setattr(session, "commit", AsyncMock(side_effect=RuntimeError("synthetic SQL failure")))
            result = await store.recover(session, paper_portfolio=portfolio(intent))
            assert result["failed"] and store.pending_addresses == {MINT}
        async with sessions() as session:
            result = await store.recover(session, paper_portfolio=portfolio(intent))
            assert result["resolved"] == [intent.intent_id]
            assert len((await session.execute(select(Position))).scalars().all()) == 1
    finally: await engine.dispose()


@pytest.mark.asyncio
async def test_lost_recovery_ack_after_successful_sql_commit_is_idempotent(tmp_path, monkeypatch):
    import runtime.buy_recovery as recovery
    engine, sessions = await database(tmp_path)
    store = BuyRecoveryStore(tmp_path / "journal")
    with store.scope():
        intent = store.begin(prototype(), paper=True, amount_sol=.1)
        intent.receive(fill(intent))
        intent.capture_position(filled_position(intent))
    writer = recovery.write_json_atomic
    def fail_ack(path, payload):
        if payload["state"] == "persisted": raise OSError("synthetic acknowledgement failure")
        writer(path, payload)
    monkeypatch.setattr(recovery, "write_json_atomic", fail_ack)
    try:
        async with sessions() as session:
            result = await store.recover(session, paper_portfolio=portfolio(intent))
            assert result["failed"] and store.pending_addresses == {MINT}
            assert len((await session.execute(select(Position))).scalars().all()) == 1
        monkeypatch.setattr(recovery, "write_json_atomic", writer)
        restarted = BuyRecoveryStore(store.directory)
        async with sessions() as session:
            assert (await restarted.recover(session, paper_portfolio=portfolio(intent)))["resolved"] == [intent.intent_id]
            assert len((await session.execute(select(Position))).scalars().all()) == 1
    finally: await engine.dispose()


def test_known_preexecution_rejection_is_durable_terminal_not_uncertain(tmp_path):
    store = BuyRecoveryStore(tmp_path / "journal")
    with store.scope():
        intent = store.begin(prototype(), paper=True, amount_sol=.1)
        intent.receive({"qty_lamports": 0, "signature": "NO_ROUTE"})
    assert not store.pending_addresses and not BuyRecoveryStore(store.directory).pending_addresses
    terminal = json.loads(next((store.directory / "resolved").glob("*.json")).read_text())
    assert terminal["state"] == "no_fill" and terminal["rejection"] == "NO_ROUTE"


def test_startup_and_monitor_recovery_precede_consumers_and_resume_cannot_bypass_pending():
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
    def calls(function, name):
        return [node.lineno for node in ast.walk(function) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == name]
    runner, monitor = functions["_runner"], functions["_check_positions"]
    assert min(calls(runner, "_recover_buy_persistence_outbox")) < min(calls(runner, "main_loop"))
    assert min(calls(monitor, "_recover_buy_persistence_outbox")) < min(calls(monitor, "_load_open_positions"))
    handler = ast.unparse(functions["_execute_control_command"])
    assert "_BUY_RECOVERY.pending_addresses" in handler and "buy_recovery_pending" in handler


@pytest.mark.asyncio
async def test_actual_resume_command_rejects_unresolved_buy_even_if_manual_pause_is_false(tmp_path):
    store = BuyRecoveryStore(tmp_path / "journal")
    with store.scope(): store.begin(prototype(), paper=True, amount_sol=.1)
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    handler = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                   and node.name == "_execute_control_command")
    namespace = {"_BUY_RECOVERY": store, "_runtime_buys_paused": False, "_runtime_discovery_paused": False,
        "_CLOSE_RECOVERY_PENDING": set(), "COMMAND_STATUS_REJECTED": "rejected", "COMMAND_STATUS_DONE": "done",
        "log": SimpleNamespace(info=lambda *a: None)}
    exec(compile(ast.Module(body=[handler], type_ignores=[]), "run_bot.py", "exec"), namespace)
    result = await namespace["_execute_control_command"]({"command_type": "resume_buys"})
    assert result == ("rejected", {"buys_paused": True, "reason": "buy_recovery_pending"}, "buy_recovery_pending")
    assert store.pending_addresses == {MINT}


@pytest.mark.asyncio
async def test_independent_entry_scopes_cannot_double_submit_while_another_owns_execution(tmp_path):
    store = BuyRecoveryStore(tmp_path / "journal")
    started, release = asyncio.Event(), asyncio.Event()
    async def first():
        with store.scope():
            store.begin(prototype(), paper=True, amount_sol=.1)
            started.set()
            await release.wait()
    task = asyncio.create_task(first())
    await started.wait()
    with store.scope(), pytest.raises(BuyRecoveryError, match="active"):
        store.begin(prototype(), paper=True, amount_sol=.1)
    assert len(store._active) == 1
    release.set()
    await task
    assert not store._active and store.pending_addresses == {MINT}


@pytest.mark.parametrize("response", [None, {}, {"qty_lamports": True}, {"qty_lamports": 2**63},
    {"qty_lamports": 0, "signature": "BUY_FAILED"},
    {"qty_lamports": 1000, "signature": "sig", "buy_price_usd": float("nan"), "entry_notional_usd": 10.}])
def test_unconfirmed_fill_does_not_become_a_known_failed_execution(tmp_path, response):
    store = BuyRecoveryStore(tmp_path / "journal")
    with store.scope():
        intent = store.begin(prototype(), paper=True, amount_sol=.1)
        with pytest.raises(BuyOutcomeUncertain): intent.receive(response)
    assert store.pending_addresses == {MINT}
    assert json.loads(next(store.directory.glob("*.json")).read_text())["state"] == "prepared"


def configure_paper(monkeypatch, tmp_path):
    from trader import papertrading as paper
    monkeypatch.setattr(paper, "CFG", replace(paper.CFG, DRY_RUN=True, PAPER_EXACT_TRADE_SIZE_ENABLED=True,
        PAPER_EXACT_TRADE_SIZE_SOL=.1, PAPER_RUNNER_RESEARCH_AUTO_APPLY=False))
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "data" / "paper_portfolio.json")
    monkeypatch.setattr(paper, "_PORTFOLIO", {})
    monkeypatch.setattr(paper, "_REQUIRE_JUP_PRICE", False)
    monkeypatch.delenv("TRADING_HOURS", raising=False)
    monkeypatch.delenv("TRADING_HOURS_EXTRA", raising=False)
    monkeypatch.setenv("PAPER_FILL_SLIPPAGE_BPS", "0")
    async def route(mint, amount_sol, *, proof):
        assert amount_sol == .1
        proof.update(out_amount=1000, in_amount=100000000, impact_bps=1, route_count=1)
        return True, "SYNTHETIC_QUOTE"
    monkeypatch.setattr(paper, "_has_jupiter_route", route)
    monkeypatch.setattr(paper, "_resolve_buy_price_usd", AsyncMock(return_value=(1., "synthetic")))
    monkeypatch.setattr(paper, "_resolve_entry_notional_usd", AsyncMock(return_value=10.))
    return paper


@pytest.mark.asyncio
async def test_paper_buy_requires_atomic_persistence_and_restores_memory_on_failure(tmp_path, monkeypatch):
    paper = configure_paper(monkeypatch, tmp_path)
    previous = {"closed": True, "historic": "preserved", "qty_lamports": 0,
        "opened_at": STAMP.isoformat(), "closed_at": STAMP.isoformat()}
    paper._PORTFOLIO[MINT] = previous
    paper._save(strict=True)
    before = paper._DATA_PATH.read_bytes()
    def fail(*args): raise OSError("synthetic disk failure")
    monkeypatch.setattr(paper, "write_json_atomic", fail)
    with pytest.raises(paper.PaperPortfolioError):
        await paper.buy(MINT, .1, entry_intent_id="a" * 32)
    assert paper._PORTFOLIO[MINT] is previous and paper._DATA_PATH.read_bytes() == before


@pytest.mark.parametrize("raw", ['{"a":', '[]', '{"a":NaN}', '{"a":1}'])
def test_corrupt_paper_portfolio_never_becomes_an_empty_first_launch(tmp_path, monkeypatch, raw):
    from trader import papertrading as paper
    path = tmp_path / "paper.json"
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(paper, "_DATA_PATH", path)
    with pytest.raises(paper.PaperPortfolioError): paper.load_portfolio()
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [True, False])
@pytest.mark.parametrize("failure", ["submission", "price_after_submission", "notional_after_submission"])
async def test_live_submission_failure_never_falls_back_or_retries(monkeypatch, managed, failure):
    from trader import buyer
    managed_call, legacy_call = AsyncMock(), AsyncMock()
    response = {"signature": "synthetic-sig", "order": {"outAmount": "1000"},
                "route": {"quote": {"outAmount": "1000"}}}
    managed_call.return_value = legacy_call.return_value = response
    chosen = managed_call if managed else legacy_call
    if failure == "submission": chosen.side_effect = TimeoutError("synthetic ambiguous submission")
    monkeypatch.setattr(buyer, "jupiter", SimpleNamespace(JUP_API_KEY="synthetic", execute_managed_swap=managed_call))
    monkeypatch.setattr(buyer, "_JUP_ROUTER_AVAILABLE", managed)
    monkeypatch.setattr(buyer.gmgn, "buy", legacy_call)
    monkeypatch.setattr(buyer, "is_in_trading_window", lambda: True)
    monkeypatch.setattr(buyer, "_max_positions_reached", AsyncMock(return_value=False))
    monkeypatch.setattr(buyer, "_has_enough_funds", AsyncMock(return_value=True))
    monkeypatch.setattr(buyer, "_has_jupiter_route", AsyncMock(return_value=(True, "synthetic")))
    monkeypatch.setattr(buyer, "_REQUIRE_JUP_PRICE", False)
    monkeypatch.setattr(buyer, "_resolve_buy_price_usd", AsyncMock(return_value=(1., "synthetic"),
        side_effect=TimeoutError("synthetic enrichment") if failure == "price_after_submission" else None))
    monkeypatch.setattr(buyer, "_resolve_entry_notional_usd", AsyncMock(return_value=10.,
        side_effect=TimeoutError("synthetic enrichment") if failure == "notional_after_submission" else None))
    with pytest.raises(BuyOutcomeUncertain): await buyer.buy(MINT, .1)
    assert chosen.await_count == 1
    assert (legacy_call if managed else managed_call).await_count == 0


def test_raw_live_token_units_never_pass_through_float_or_bool():
    from trader import buyer
    quantity = 2**53 + 123
    assert buyer._raw_token_units(str(quantity)) == quantity
    assert buyer._raw_token_units(quantity) == quantity
    assert buyer._raw_token_units(float(quantity)) == 0 and buyer._raw_token_units(True) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("balance", [None, True, -1, float("nan"), "1000000000"])
async def test_unknown_live_balance_never_authorizes_a_buy(monkeypatch, balance):
    from trader import buyer
    monkeypatch.setattr(buyer, "_WALLET_PUBKEY", "synthetic-wallet")
    monkeypatch.setattr(buyer, "get_balance_lamports", AsyncMock(return_value=balance))
    assert await buyer._has_enough_funds(.1) is False


@pytest.mark.asyncio
async def test_live_balance_failure_missing_identity_and_gas_reserve_are_fail_closed(monkeypatch):
    from trader import buyer
    rpc = AsyncMock(side_effect=TimeoutError("synthetic RPC failure"))
    monkeypatch.setattr(buyer, "get_balance_lamports", rpc)
    monkeypatch.setattr(buyer, "_WALLET_PUBKEY", "")
    assert await buyer._has_enough_funds(.1) is False
    rpc.assert_not_called()
    monkeypatch.setattr(buyer, "_WALLET_PUBKEY", "synthetic-wallet")
    assert await buyer._has_enough_funds(.1) is False
    rpc.side_effect = None
    monkeypatch.setattr(buyer, "_GAS_RESERVE_LAMPORTS", 25000)
    rpc.return_value = 100024999
    assert await buyer._has_enough_funds(.1) is False
    rpc.return_value = 100025000
    assert await buyer._has_enough_funds(.1) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [float("nan"), float("inf"), True])
async def test_invalid_live_amount_rejected_before_any_submission(monkeypatch, amount):
    from trader import buyer
    execution = AsyncMock()
    monkeypatch.setattr(buyer.gmgn, "buy", execution)
    response = await buyer.buy(MINT, amount)
    assert response["signature"] == "INVALID_AMOUNT"
    execution.assert_not_called()


def execution_tail_namespace(tmp_path, store, paper):
    from analytics import runner_ladder
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    builder = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_build_entry_position")
    entry = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_evaluate_and_buy")
    begin = next(index for index, node in enumerate(entry.body) if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_build_entry_position") - 3
    end = next(index for index, node in enumerate(entry.body) if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "confirm")
    tail = ast.AsyncFunctionDef(name="execution_tail", args=ast.arguments(posonlyargs=[],
        args=[ast.arg(arg="token"), ast.arg(arg="ses")], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=entry.body[begin:end+1], decorator_list=[])
    namespace = {"Position": Position, "DRY_RUN": True, "_BUY_RECOVERY": store, "buyer": paper,
        "_runner_profile_for_subject": lambda token: "frozen-runner", "get_runtime_context": lambda: {"run_id": "synthetic-run"},
        "parse_iso_utc": dt.datetime.fromisoformat, "utc_now": lambda: STAMP,
        "_metric_int": lambda token, key: token.get(key), "_is_liquidity_proxy": lambda token: False,
        "runner_ladder": runner_ladder, "_config_hash": lambda: "synthetic-hash", "addr": MINT,
        "size_decision": SimpleNamespace(regime="pump_early", bucket="base", multiplier=1.), "amount_sol": .1,
        "proba": .6, "paper_bootstrap_decision": None, "paper_bootstrap_fast_path": False,
        "require_jup_for_buy": False, "_stats": {"actual_paper_buy_attempts": 0, "actual_paper_buys": 0},
        "_record_paper_bootstrap_event": lambda *a, **k: None, "strategy_runtime": SimpleNamespace(record_execution=lambda *a: None),
        "log_execution_event": lambda *a, **k: None, "_research_decision": lambda *a, **k: None,
        "_note_runtime_error": lambda *a: None, "_pending_ai_vectors": {}, "_remove_from_queue_if_present": lambda *a: None,
        "log": SimpleNamespace(error=lambda *a: None), "ai_threshold_eff": .5, "rank_info": {}}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[builder, tail], type_ignores=[])), "run_bot.py", "exec"), namespace)
    return namespace


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "after_fill", "sql_commit"])
async def test_actual_entry_execution_tail_and_restart_recovery(tmp_path, monkeypatch, failure):
    paper = configure_paper(monkeypatch, tmp_path)
    store = BuyRecoveryStore(tmp_path / "journal")
    engine, sessions = await database(tmp_path)
    namespace = execution_tail_namespace(tmp_path, store, paper)
    if failure == "after_fill":
        def fail(*args, **kwargs):
            if kwargs.get("action") == "bought": raise asyncio.CancelledError()
        namespace["_research_decision"] = fail
    try:
        async with sessions() as seed:
            seed.add(Token(address=MINT))
            await seed.commit()
        with store.scope():
            async with sessions() as session:
                if failure == "sql_commit": monkeypatch.setattr(session, "commit", AsyncMock(side_effect=RuntimeError("synthetic commit")))
                if failure == "none": await namespace["execution_tail"]({"address": MINT, "symbol": "ORIGINAL"}, session)
                else:
                    with pytest.raises(asyncio.CancelledError if failure == "after_fill" else RuntimeError):
                        await namespace["execution_tail"]({"address": MINT, "symbol": "ORIGINAL"}, session)
        restarted = BuyRecoveryStore(store.directory)
        async with sessions() as session:
            result = await restarted.recover(session, paper_portfolio=paper.load_portfolio())
            assert len(result["resolved"]) == (0 if failure == "none" else 1)
            assert not result["failed"] and not restarted.pending_addresses
            position = (await session.execute(select(Position))).scalar_one()
            assert position.qty == 1000 and position.symbol == "ORIGINAL" and position.buy_amount_sol == .1
            assert position.entry_notional_usd == 10. and position.run_id == "synthetic-run"
            assert position.source_position_key.startswith("buy:")
        assert len(paper._PORTFOLIO) == 1
    finally: await engine.dispose()
