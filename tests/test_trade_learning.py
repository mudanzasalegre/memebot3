"""Frozen T0 -> checked paper receipts -> restart-safe net labels, isolated only."""
from __future__ import annotations

import ast
import asyncio
import threading
import copy
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock

import pandas as pd
import pyarrow.parquet as pq
import pytest
import pytest_asyncio

from db.models import Position
from features import store
from features.builder import COLUMNS, ALLOWED_FEATURES
from ml.financial_targets import apply_checked_net_returns, checked_net_return
from ml.label_builder import attach_labels
from runtime import trade_learning as learning
from runtime.buy_recovery import BuyRecoveryStore
from runtime.paper_archive import entry_identity
from utils.atomic_json import read_json_strict, write_json_atomic
from test_paper_archive import paper

STAMP = dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc)
MINT = "A" * 32


def vector():
    return {"address": MINT, "timestamp": STAMP - dt.timedelta(seconds=1),
        "score_total": 70, "entry_regime": "pump_early", "entry_lane": "runner",
        "private_key": "not-a-real-secret", "price_pct_5m": float("nan")}


def runtime_namespace(root, *, pending=None):
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_persist_dataset_at_close")
    ns = {"Position": Position, "Optional": Optional, "PROJECT_ROOT": root,
        "_pending_ai_vectors": pending or {}, "_stats": {"appended_at_close": 0},
        "log": SimpleNamespace(warning=lambda *a: None)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "run_bot.py", "exec"), ns)
    return ns


@pytest.fixture(autouse=True)
def clean_learning(monkeypatch):
    monkeypatch.setattr(learning, "_REPAIR_STATE", {})


@pytest_asyncio.fixture
async def closed(paper, tmp_path, monkeypatch, request):
    from runtime import buy_recovery
    clock = [STAMP]
    monkeypatch.setattr(paper, "utc_now", lambda: clock[0])
    monkeypatch.setattr(buy_recovery, "_now", lambda: clock[0].isoformat())
    monkeypatch.setattr(paper, "runtime_context_payload", lambda: {"run_id": "net-label-test", "run_started_at": STAMP.isoformat()})
    monkeypatch.setenv("PAPER_FILL_FEE_SOL", "0.002")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "data" / "features")
    journal = BuyRecoveryStore(tmp_path / "data" / "metrics" / "buy_recovery")
    prototype = Position(address=MINT, token_mint=MINT, qty=0, entry_qty=0, buy_price_usd=0.,
        buy_amount_sol=.1, entry_notional_usd=0., dry_run=True, opened_at=STAMP, run_id="net-label-test")
    with journal.scope():
        attempt = journal.begin(prototype, paper=True, amount_sol=.1, feature_vector=vector(), positive_pnl_ratio=.1)
        response = await paper.buy(MINT, .1, entry_intent_id=attempt.intent_id)
        attempt.receive(response)
        pos = Position(id=1, address=MINT, token_mint=MINT, qty=response["qty_lamports"],
            entry_qty=response["qty_lamports"], buy_price_usd=response["buy_price_usd"],
            buy_tx_sig=response["signature"], buy_amount_sol=.1, entry_notional_usd=response["entry_notional_usd"],
            dry_run=True, opened_at=STAMP, run_id="net-label-test")
        attempt.capture_position(pos)
        attempt.confirm(pos)
    clock[0] += dt.timedelta(minutes=1)
    async def reverse(**kwargs):
        return SimpleNamespace(ok=True, in_amount=kwargs["amount_lamports"], out_amount=getattr(request, "param", 101000000), price_impact_bps=1)
    monkeypatch.setattr(paper.jupiter_router, "get_quote", AsyncMock(side_effect=reverse))
    paper._PORTFOLIO[MINT]["max_pnl_pct_seen"] = 10000.
    result = await paper.sell(MINT, pos.qty, exit_intent_id="c" * 32)
    assert result["ok"]
    pos.closed, pos.qty, pos.closed_at = True, 0, clock[0]
    pos.total_pnl_pct, pos.close_price_usd = 99999., 99999.  # Deliberately untrusted SQL/spot values.
    return SimpleNamespace(root=tmp_path, identity=attempt.intent_id, pos=pos, journal=journal, paper=paper, clock=clock)


def dataset(root):
    return pd.read_parquet(next((root / "data" / "features").glob("features_*.parquet")))


def source_path(case):
    return learning._directory(case.root) / (case.identity + ".json")


def test_entry_features_are_detached_whitelisted_and_keep_missingness():
    original = vector()
    payload = learning.freeze_entry_features(original, address=MINT, captured_at=STAMP, positive_pnl_ratio=.1)
    original["score_total"] = 999
    assert payload["vector"]["score_total"] == 70 and payload["vector"]["price_pct_5m"] is None
    assert set(payload["vector"]) == set(COLUMNS) and "not-a-real-secret" not in json.dumps(payload)
    with pytest.raises(learning.TradeLearningError):
        learning.freeze_entry_features(original, address="other", captured_at=STAMP)
    with pytest.raises(learning.TradeLearningError):
        learning.freeze_entry_features(original, address=MINT, captured_at=STAMP - dt.timedelta(seconds=2))


def test_actual_pre_buy_path_freezes_original_features_before_submission():
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    begin = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_BUY_RECOVERY" and node.func.attr == "begin")
    keywords = {item.arg: item.value for item in begin.keywords}
    assert isinstance(keywords["feature_vector"], ast.Call)
    assert keywords["feature_vector"].func.id == "_entry_vector_for_close"
    assert keywords["feature_vector"].args[0].id == "vec"
    assert "positive_pnl_ratio" in keywords
    buys = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "buyer" and node.func.attr == "buy"]
    assert buys and all(begin.lineno < node.lineno for node in buys)


@pytest.mark.asyncio
async def test_actual_close_writer_uses_net_not_sql_spot_or_observed_extreme_peak(closed):
    ns = runtime_namespace(closed.root, pending={MINT: vector()})
    ns["_persist_dataset_at_close"](closed.pos, 99999.)
    assert source_path(closed).exists() and not list((closed.root / "data" / "features").glob("*.parquet"))
    learning.publish_close(closed.identity, root=closed.root)
    row = dataset(closed.root).iloc[0]
    assert row["target_total_pnl_pct"] == pytest.approx(-3., abs=1e-6)
    assert row["outcome_gross_pnl_pct"] == pytest.approx(1.) and row["label"] == 0
    assert row["max_pnl_pct_seen"] == 10000 and row["score_total"] == 70
    assert ns["_stats"]["appended_at_close"] == 0 and not ns["_pending_ai_vectors"]
    assert checked_net_return(row) == pytest.approx(-3.)
    labels = attach_labels(pd.DataFrame([{**row, "total_pnl_pct": 99999., "realized_pnl_pct": 99999.}]))
    assert labels.iloc[0]["is_winner"] == 0 and labels.iloc[0]["ev_realized"] == pytest.approx(-3.)
    assert labels.iloc[0]["runner_10000"] == 1


@pytest.mark.asyncio
async def test_retry_is_idempotent_and_preserves_original_month_and_frozen_threshold(closed):
    assert learning.publish_close(closed.identity, root=closed.root)["status"] == "written"
    before = source_path(closed).read_bytes()
    assert learning.publish_close(closed.identity, root=closed.root)["status"] == "already_written"
    assert len(dataset(closed.root)) == 1 and source_path(closed).read_bytes() == before
    assert [p.name for p in (closed.root / "data" / "features").glob("*.parquet")] == ["features_202610.parquet"]
    assert read_json_strict(source_path(closed))["entry_features"]["positive_pnl_ratio"] == .1


@pytest.mark.asyncio
async def test_parquet_failure_retains_retryable_source_and_actual_financial_close(closed, monkeypatch):
    writer = store._atomic_write_table
    def fail(*a, **k): raise OSError("synthetic disk failure")
    monkeypatch.setattr(store, "_atomic_write_table", fail)
    ns = runtime_namespace(closed.root, pending={MINT: vector()})
    ns["_persist_dataset_at_close"](closed.pos, 99999.)
    assert source_path(closed).exists() and not ns["_pending_ai_vectors"]
    with pytest.raises(OSError): learning.publish_close(closed.identity, root=closed.root)
    assert closed.paper._PORTFOLIO[MINT]["closed"] and ns["_stats"]["appended_at_close"] == 0
    quote_calls = closed.paper.jupiter_router.get_quote.await_count
    monkeypatch.setattr(store, "_atomic_write_table", writer)
    result = learning.repair_exports(root=closed.root, cfg=SimpleNamespace(DRY_RUN=True), force=True)
    assert result == {"status": "ok", "attempted": 1, "failed": 0, "written": 1}
    assert dataset(closed.root).iloc[0]["target_total_pnl_pct"] == pytest.approx(-3.)
    assert closed.paper.jupiter_router.get_quote.await_count == quote_calls


@pytest.mark.asyncio
async def test_response_loss_after_parquet_write_does_not_duplicate_row(closed, monkeypatch):
    append = store.append
    def lose_response(*a, **k):
        append(*a, **k)
        raise OSError("synthetic response loss")
    monkeypatch.setattr(store, "append", lose_response)
    with pytest.raises(OSError): learning.publish_close(closed.identity, root=closed.root)
    monkeypatch.setattr(store, "append", append)
    assert learning.publish_close(closed.identity, root=closed.root)["status"] == "already_written"
    assert len(dataset(closed.root)) == 1


@pytest.mark.asyncio
async def test_closed_reentry_and_empty_memory_recover_original_entry_not_future_vector(closed, monkeypatch):
    await closed.paper.buy(MINT, .1, entry_intent_id="b" * 32)
    assert entry_identity(closed.paper._PORTFOLIO[MINT]) != closed.identity
    newer = {**vector(), "timestamp": STAMP + dt.timedelta(minutes=2), "score_total": 999}
    ns = runtime_namespace(closed.root, pending={MINT: newer})
    ns["_persist_dataset_at_close"](closed.pos, None)
    learning.publish_close(closed.identity, root=closed.root)
    assert dataset(closed.root).iloc[0]["score_total"] == 70
    assert ns["_pending_ai_vectors"][MINT]["score_total"] == 999
    ns["_persist_dataset_at_close"](closed.pos, None)
    assert len(dataset(closed.root)) == 1 and ns["_stats"]["appended_at_close"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing_features", "corrupt_features", "missing_journal", "open", "archive_corrupt"])
async def test_missing_or_corrupt_original_inputs_never_fabricate_a_label(closed, mutation):
    journal = next((closed.root / "data" / "metrics" / "buy_recovery" / "resolved").glob("*.json"))
    if mutation == "missing_journal": journal.unlink()
    elif mutation in {"missing_features", "corrupt_features"}:
        row = read_json_strict(journal)
        if mutation == "missing_features": row.pop("entry_features")
        else: row["entry_features"]["vector"]["score_total"] = 999
        write_json_atomic(journal, row)
    else:
        archive = closed.root / "data" / "paper_closed_trades" / (closed.identity + ".json")
        if mutation == "archive_corrupt": archive.write_text('{"torn":')
        else:
            archive.unlink()
            row = read_json_strict(closed.paper._DATA_PATH)
            row[MINT]["closed"] = False
            write_json_atomic(closed.paper._DATA_PATH, row)
    with pytest.raises((RuntimeError, ValueError, KeyError)):
        learning.publish_close(closed.identity, root=closed.root)
    assert not list((closed.root / "data" / "features").glob("*.parquet"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["net", "receipt_qty", "receipt_signature", "missing_receipt", "future_features", "buy_lineage"])
async def test_checked_source_rejects_rehashed_accounting_or_causal_conflicts(closed, mutation):
    source = learning._capture(closed.identity, root=closed.root)
    if mutation == "net": source["trade"]["net_total_pnl_usd"] = 999.
    elif mutation == "receipt_qty": source["trade"]["exit_fill_events"][0]["response"]["qty_sold"] = 999
    elif mutation == "receipt_signature": source["trade"]["exit_fill_events"][0]["response"]["signature"] = "LIVE"
    elif mutation == "missing_receipt": source["trade"]["exit_fill_events"] = []
    elif mutation == "future_features":
        source["entry_features"]["captured_at"] = (STAMP + dt.timedelta(minutes=1)).isoformat()
        entry = source["entry_features"]
        entry["payload_sha256"] = learning._hash({k: v for k, v in entry.items() if k != "payload_sha256"})
    else: source["buy_proof"]["fill"]["signature"] = "SIM-" + "b" * 32
    source["payload_sha256"] = learning._hash({k: v for k, v in source.items() if k != "payload_sha256"})
    with pytest.raises(RuntimeError): learning.validate_source(source)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["basis", "checksum", "target", "identity", "time", "sample"])
async def test_bad_declared_net_proof_is_unknown_not_gross_fallback(closed, mutation):
    learning.publish_close(closed.identity, root=closed.root)
    row = dataset(closed.root).iloc[0].to_dict()
    if mutation == "basis": row["outcome_return_basis"] = "unsupported"
    elif mutation == "checksum": row["outcome_source_sha256"] = "corrupt"
    elif mutation == "target": row["target_total_pnl_pct"] = 999.
    elif mutation == "identity": row["outcome_trade_id"] = "b" * 32
    elif mutation == "time": row["timestamp"] = STAMP + dt.timedelta(seconds=1)
    else: row["sample_type"] = "shadow_close"
    row["total_pnl_pct"], row["realized_pnl_pct"], row["label"] = 99999., 99999., 1
    result = attach_labels(pd.DataFrame([row]))
    assert pd.isna(result.iloc[0]["is_winner"]) and pd.isna(result.iloc[0]["label"])
    assert pd.isna(result.iloc[0]["runner_10000"])


@pytest.mark.asyncio
async def test_model_loader_revalidates_proof_and_duplicate_dataframe_indexes(closed, monkeypatch):
    learning.publish_close(closed.identity, root=closed.root)
    row = dataset(closed.root).iloc[0].to_dict()
    duplicate = pd.DataFrame([row, {**row, "outcome_source_sha256": "bad"}], index=[5, 5])
    sanitized = apply_checked_net_returns(duplicate)
    assert sanitized.index.tolist() == [5, 5] and sanitized.iloc[0]["label"] == 0 and pd.isna(sanitized.iloc[1]["label"])
    from ml import train
    monkeypatch.setattr(train, "DATA_DIR", closed.root / "data" / "features")
    loaded = train._load_dataset()
    assert loaded.iloc[0]["target_total_pnl_pct"] == pytest.approx(-3.) and loaded.iloc[0]["label"] == 0
    assert all(column not in ALLOWED_FEATURES for column in store._OUTCOME_COLS)


@pytest.mark.asyncio
async def test_existing_conflicting_or_corrupt_parquet_is_preserved(closed):
    learning.publish_close(closed.identity, root=closed.root)
    path = next((closed.root / "data" / "features").glob("*.parquet"))
    table = pq.read_table(path)
    column = table.column("label").to_pylist()
    column[0] = 1
    import pyarrow as pa
    pq.write_table(table.set_column(table.schema.get_field_index("label"), "label", pa.array(column, type=pa.int8())), path)
    previous = path.read_bytes()
    with pytest.raises(ValueError): learning.publish_close(closed.identity, root=closed.root)
    assert path.read_bytes() == previous
    path.write_bytes(b"synthetic corrupt parquet")
    with pytest.raises(Exception): learning.publish_close(closed.identity, root=closed.root)
    assert path.read_bytes() == b"synthetic corrupt parquet" and source_path(closed).exists()


@pytest.mark.asyncio
async def test_legacy_update_cannot_mutate_checked_causal_close(closed, monkeypatch):
    learning.publish_close(closed.identity, root=closed.root)
    path = next((closed.root / "data" / "features").glob("*.parquet"))
    monkeypatch.setattr(store, "_file_for_now", lambda *a: path)
    previous = path.read_bytes()
    store.update_pnl(MINT, 99999.)
    assert path.read_bytes() == previous


def test_disabled_empty_and_failed_rotating_export_are_bounded(tmp_path, monkeypatch):
    assert learning.repair_exports(root=tmp_path, cfg=SimpleNamespace(DRY_RUN=False), force=True)["status"] == "disabled"
    assert learning.repair_exports(root=tmp_path, cfg=SimpleNamespace(DRY_RUN=True), force=True)["attempted"] == 0
    assert not list(tmp_path.iterdir())
    for index in range(10): write_json_atomic(learning._directory(tmp_path) / (f"{index:032x}" + ".json"), {})
    calls = []
    def fail(identity, **kwargs):
        calls.append(identity)
        raise learning.TradeLearningError("synthetic")
    monkeypatch.setattr(learning, "publish_close", fail)
    cfg = SimpleNamespace(DRY_RUN=True)
    assert learning.repair_exports(root=tmp_path, cfg=cfg, force=True, limit=2)["failed"] == 2
    assert learning.repair_exports(root=tmp_path, cfg=cfg)["status"] == "throttled"
    learning.repair_exports(root=tmp_path, cfg=cfg, force=True, limit=2)
    assert calls == [f"{i:032x}" for i in range(4)]


def test_live_close_never_exports_estimated_paper_target(tmp_path, monkeypatch):
    def fail(*a, **k): raise AssertionError("Live close must not consume paper exporter")
    monkeypatch.setattr(learning, "prepare_close", fail)
    ns = runtime_namespace(tmp_path)
    ns["_persist_dataset_at_close"](Position(address=MINT, dry_run=False, closed=True), 99999.)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_export_owner_waits_runs_off_thread_and_drains_on_shutdown(tmp_path, monkeypatch):
    ready, started, finished = asyncio.Event(), threading.Event(), threading.Event()
    release = threading.Event()
    main_thread, owner_thread, calls, results = threading.get_ident(), [], [], []
    def blocked(**kwargs):
        owner_thread.append(threading.get_ident())
        calls.append(kwargs)
        started.set()
        assert release.wait(timeout=3)
        finished.set()
        return {"status": "ok", "failed": 0, "written": 1}
    monkeypatch.setattr(learning, "repair_exports", blocked)
    owner = asyncio.create_task(learning.run_export_loop(ready=ready, root=tmp_path,
        cfg=SimpleNamespace(DRY_RUN=True), on_result=results.append, on_error=lambda exc: pytest.fail(str(exc))))
    await asyncio.sleep(0)
    assert not started.is_set()
    ready.set()
    while not started.is_set(): await asyncio.sleep(.001)
    # The event loop continues to tick while the filesystem owner is blocked.
    assert owner_thread != [main_thread] and len(calls) == 1
    owner.cancel()
    await asyncio.sleep(.01)
    assert not owner.done() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError): await owner
    assert finished.is_set() and not results and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [101000000, 120000000], indirect=True)
async def test_real_paper_positive_and_negative_net_labels_use_frozen_threshold(closed):
    learning.publish_close(closed.identity, root=closed.root)
    row = dataset(closed.root).iloc[0]
    expected = "win" if row["target_total_pnl_pct"] >= 10 else "fail"
    assert learning.checked_position_outcome(closed.pos, root=closed.root) == expected
    assert row["label"] == (1 if expected == "win" else 0)
    assert row["target_total_pnl_pct"] < row["outcome_gross_pnl_pct"]


@pytest.mark.asyncio
async def test_sql_periodic_labels_require_terminal_net_proof_and_do_not_mark_open_timeout(closed, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from db.database import Base
    from db.models import Token
    from labeler import win_labeler
    engine = create_async_engine(f"sqlite+aiosqlite:///{(closed.root / 'labels.db').as_posix()}")
    async with engine.begin() as connection: await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(win_labeler, "SessionLocal", sessions)
    monkeypatch.setattr(win_labeler, "PROJECT_ROOT", closed.root)
    try:
        async with sessions() as session:
            session.add(Token(address=MINT))
            session.add(closed.pos)
            session.add(Position(id=2, address=MINT, qty=0, entry_qty=1000, buy_price_usd=1., dry_run=True,
                closed=True, closed_at=STAMP + dt.timedelta(hours=1), opened_at=STAMP,
                total_pnl_pct=99999., source_position_key=None))
            session.add(Position(id=3, address=MINT, qty=1000, entry_qty=1000, buy_price_usd=1.,
                dry_run=True, closed=False, opened_at=STAMP - dt.timedelta(days=30)))
            await session.commit()
        await win_labeler.label_positions()
        async with sessions() as session:
            assert (await session.get(Position, 1)).outcome == "fail"
            assert (await session.get(Position, 2)).outcome is None
            assert (await session.get(Position, 3)).outcome is None
    finally: await engine.dispose()


@pytest.mark.asyncio
async def test_primary_learning_source_failure_keeps_memory_for_retry(closed, monkeypatch):
    def fail(*a, **k): raise OSError("synthetic source disk failure")
    monkeypatch.setattr(learning, "write_json_atomic", fail)
    ns = runtime_namespace(closed.root, pending={MINT: vector()})
    ns["_persist_dataset_at_close"](closed.pos, None)
    assert not source_path(closed).exists() and ns["_pending_ai_vectors"]
    assert closed.paper._PORTFOLIO[MINT]["closed"]


@pytest.mark.asyncio
async def test_same_mint_two_managed_closes_keep_two_original_causal_rows(closed):
    learning.publish_close(closed.identity, root=closed.root)
    closed.clock[0] += dt.timedelta(minutes=1)
    t0 = closed.clock[0]
    proto = Position(address=MINT, token_mint=MINT, qty=0, entry_qty=0, buy_price_usd=0.,
        buy_amount_sol=.1, entry_notional_usd=0., dry_run=True, opened_at=t0, run_id="net-label-test")
    with closed.journal.scope():
        attempt = closed.journal.begin(proto, paper=True, amount_sol=.1,
            feature_vector={**vector(), "timestamp": t0 - dt.timedelta(seconds=1), "score_total": 31}, positive_pnl_ratio=.1)
        response = await closed.paper.buy(MINT, .1, entry_intent_id=attempt.intent_id)
        attempt.receive(response)
        pos = Position(id=2, address=MINT, token_mint=MINT, qty=response["qty_lamports"], entry_qty=response["qty_lamports"],
            buy_price_usd=response["buy_price_usd"], buy_tx_sig=response["signature"], buy_amount_sol=.1,
            entry_notional_usd=response["entry_notional_usd"], dry_run=True, opened_at=t0, run_id="net-label-test")
        attempt.capture_position(pos)
        attempt.confirm(pos)
    closed.clock[0] += dt.timedelta(minutes=1)
    assert (await closed.paper.sell(MINT, pos.qty))["ok"]
    learning.publish_close(attempt.intent_id, root=closed.root)
    rows = dataset(closed.root)
    assert len(rows) == 2 and rows.outcome_trade_id.nunique() == 2
    assert rows.score_total.tolist() == [70, 31]
    assert rows.max_pnl_pct_seen.tolist() == [10000, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["raw_float", "future_close", "boolean_qty"])
async def test_non_native_raw_quote_or_future_outcome_cannot_be_rehashed_into_a_valid_label(closed, mutation):
    source = learning._capture(closed.identity, root=closed.root)
    if mutation == "raw_float": source["trade"]["entry_route_quote"]["out_amount"] = 1000.
    elif mutation == "future_close": source["trade"]["closed_at"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    else: source["buy_proof"]["fill"]["qty_lamports"] = True
    source["payload_sha256"] = learning._hash({k: v for k, v in source.items() if k != "payload_sha256"})
    with pytest.raises(RuntimeError): learning.validate_source(source)


def test_unreadable_export_directory_is_not_empty_and_failure_is_throttled(tmp_path, monkeypatch):
    original = Path.iterdir
    def blocked(path):
        if path == learning._directory(tmp_path): raise PermissionError("synthetic unreadable source")
        return original(path)
    monkeypatch.setattr(Path, "iterdir", blocked)
    cfg = SimpleNamespace(DRY_RUN=True)
    assert learning.repair_exports(root=tmp_path, cfg=cfg, force=True) == {
        "status": "pending", "attempted": 0, "failed": 1, "source_scan_failed": True}
    assert learning.repair_exports(root=tmp_path, cfg=cfg)["status"] == "throttled"


def test_legacy_gross_diagnostics_are_preserved_without_relabelling_them_as_net():
    frame = pd.DataFrame([{"sample_type": "shadow_close", "target_total_pnl_pct": 20., "label": 1}])
    assert apply_checked_net_returns(frame).equals(frame)
    assert "outcome_return_basis" not in frame
