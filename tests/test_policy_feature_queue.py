"""Native policy-reject persistence must not hold the candidate event loop."""
import ast
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import time
import asyncio
import datetime as dt
import threading
from copy import deepcopy
import pandas as pd
import numpy as np
import pytest


def native_reject(writer):
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                and item.name == "_store_policy_reject")
    ns = {"time": time, "_sample_address": lambda row: row.get("address", ""),
        "build_feature_vector": lambda row: dict(row), "_POLICY_REJECT_DEDUP_TTL_S": 60.,
        "_policy_reject_seen": {}, "_stats": defaultdict(int),
        "_POLICY_FEATURE_WRITER": writer, "_note_runtime_error": Mock(),
        "log": SimpleNamespace(warning=Mock(), debug=Mock()),
        "store_append": Mock(side_effect=AssertionError("disk writer entered candidate event loop"))}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "run_bot.py", "exec"), ns)
    return ns


def test_native_reject_admits_without_calling_disk_writer_inline():
    writer = SimpleNamespace(submit=Mock(return_value=True))
    ns = native_reject(writer)
    ns["_store_policy_reject"]({"address": "synthetic-candidate"}, reason="basic_filter")
    ns["store_append"].assert_not_called()
    writer.submit.assert_called_once()


def test_unadmitted_reject_does_not_poison_future_retry_dedup():
    writer = SimpleNamespace(submit=Mock(return_value=False))
    ns = native_reject(writer)
    ns["store_append"] = Mock(return_value=True)
    ns["_store_policy_reject"]({"address": "synthetic-candidate"}, reason="basic_filter")
    assert ns["_policy_reject_seen"] == {}
    assert ns["_stats"]["feature_policy_reject_dropped"] == 1


@pytest.mark.asyncio
async def test_owned_writer_does_not_hold_loop_or_next_candidate_and_bounds_outstanding():
    from features.policy_write_queue import PolicyWriteQueue
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()
    rows, results = [], []
    owner_thread = threading.get_ident()
    def writer(row, label, **kwargs):
        assert threading.get_ident() != owner_thread
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        rows.append((row["address"], label, kwargs))
        return True
    queue = PolicyWriteQueue(writer, capacity=2, on_result=lambda *args: results.append(args))
    queue.start()
    try:
        assert queue.submit({"address": "first"})
        await asyncio.wait_for(started.wait(), 2)
        # A real blocked native thread does not prevent another loop turn or admission.
        await asyncio.sleep(0)
        assert queue.submit({"address": "second"})
        assert not queue.submit({"address": "overflow"})
        assert queue.snapshot()["pending"] == 2 and queue.persisted == 0
        close = asyncio.create_task(queue.close())
        await asyncio.sleep(0)
        assert not close.done()
        release.set()
        await close
    finally:
        release.set()
        await queue.close()
    assert [row[0] for row in rows] == ["first", "second"]
    assert all(label == 0 and kw["sample_type"] == "policy_reject" and kw["strict"] for _, label, kw in rows)
    assert queue.snapshot() == {"admitted": 2, "persisted": 2, "failed": 0, "dropped": 1,
        "pending": 0, "accepting": False, "closed": True, "last_drop_reason": "capacity", "callback_errors": 0}
    assert len(results) == 2 and all(result[1] is True for result in results)


@pytest.mark.asyncio
async def test_snapshot_preserves_original_t0_proof_and_context_before_later_mutation():
    from features.policy_write_queue import PolicyWriteQueue
    from features import store
    t0 = dt.datetime(2026, 9, 30, 23, 59, tzinfo=dt.timezone.utc)
    vector = pd.Series({"address": "snapshot", "timestamp": t0, "liquidity_usd": np.float64(50000)})
    vector.attrs[store.CLOCK_PROOF_COLUMN] = "original-clock-proof"
    context = {"source": ["original"]}
    rows, contexts = [], []
    queue = PolicyWriteQueue(lambda row, *args, **kw: rows.append((deepcopy(row), kw)) or True,
        on_result=lambda ctx, *args: contexts.append(ctx))
    queue.start()
    assert queue.submit(vector, context=context)
    vector["timestamp"] = t0 + dt.timedelta(days=1)
    vector.attrs[store.CLOCK_PROOF_COLUMN] = "later-proof"
    context["source"].append("later")
    await queue.close()
    assert rows[0][0]["timestamp"] == t0
    assert rows[0][0][store.CLOCK_PROOF_COLUMN] == "original-clock-proof"
    assert contexts == [{"source": ["original"]}]
    assert rows[0][1]["partition_at"].tzinfo is dt.timezone.utc


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "false", "none"])
async def test_failed_write_is_not_persistence_and_later_job_still_runs(failure):
    from features.policy_write_queue import PolicyWriteQueue
    calls, results = [], []
    def writer(row, *args, **kwargs):
        calls.append(row["address"])
        if row["address"] == "bad":
            if failure == "exception": raise OSError("synthetic IO error")
            return False if failure == "false" else None
        return True
    queue = PolicyWriteQueue(writer, on_result=lambda *args: results.append(args))
    queue.start()
    assert queue.submit({"address": "bad"}) and queue.submit({"address": "good"})
    await queue.close()
    assert calls == ["bad", "good"]
    assert queue.failed == 1 and queue.persisted == 1 and queue.snapshot()["pending"] == 0
    assert results[0][1] is False and isinstance(results[0][2], Exception)
    assert results[1][1] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["x" * 70000, np.str_("x" * 70000), np.void(b"x" * 70000),
    np.arange(20), 10**600000], ids=["unicode", "numpy-unicode", "numpy-void", "array", "huge-int"])
async def test_oversized_or_unsupported_payload_is_rejected_before_worker(value):
    from features.policy_write_queue import PolicyWriteQueue
    writer = Mock(return_value=True)
    queue = PolicyWriteQueue(writer)
    queue.start()
    assert not queue.submit({"address": "invalid", "liquidity_usd": value})
    await queue.close()
    writer.assert_not_called()
    assert queue.dropped == 1 and queue.last_drop_reason == "invalid_snapshot"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["outcome_trade_id", "outcome_execution_proof", "outcome_return_basis"])
async def test_financial_close_identifiers_cannot_be_laundered_into_policy_zero(field):
    from features.policy_write_queue import PolicyWriteQueue
    writer = Mock(return_value=True)
    queue = PolicyWriteQueue(writer)
    queue.start()
    assert not queue.submit({"address": "financial", field: "nonempty"})
    await queue.close()
    writer.assert_not_called()


@pytest.mark.asyncio
async def test_foreign_thread_and_loop_cannot_admit_or_mutate_owner():
    from features.policy_write_queue import PolicyWriteQueue
    queue = PolicyWriteQueue(lambda *args, **kw: True)
    queue.start()
    before = queue.snapshot()
    def foreign():
        async def attempt(): return queue.submit({"address": "foreign-loop"})
        assert not queue.submit({"address": "foreign-thread"})
        return asyncio.run(attempt())
    assert await asyncio.to_thread(foreign) is False
    assert queue.snapshot() == before
    await queue.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_worker", [False, True])
async def test_repeated_cancel_drains_admitted_jobs_and_executor(cancel_worker):
    from features.policy_write_queue import PolicyWriteQueue
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    rows = []
    def writer(row, *args, **kw):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        rows.append(row["address"])
        return True
    queue = PolicyWriteQueue(writer)
    queue.start()
    assert queue.submit({"address": "first"}) and queue.submit({"address": "second"})
    await asyncio.wait_for(started.wait(), 2)
    closer = asyncio.create_task(queue.close())
    try:
        await asyncio.sleep(0)
        target = queue._task if cancel_worker else closer
        target.cancel()
        await asyncio.sleep(0)
        target.cancel()
        await asyncio.sleep(0)
        assert not closer.done() and not queue.snapshot()["closed"]
        release.set()
        if cancel_worker:
            await closer
        else:
            with pytest.raises(asyncio.CancelledError): await closer
    finally:
        release.set()
        await asyncio.gather(closer, return_exceptions=True)
        await queue.close()
    assert rows == ["first", "second"] and queue.persisted == 2
    assert queue.snapshot()["closed"] and queue.snapshot()["pending"] == 0
    assert not any(thread.name.startswith("memebot-policy-writer") for thread in threading.enumerate())


@pytest.mark.parametrize("value", [0, -1, True, 8193, None, 1.5])
def test_invalid_capacity_never_creates_worker(value):
    from features.policy_write_queue import PolicyWriteQueue
    with pytest.raises(ValueError, match="capacity"):
        PolicyWriteQueue(capacity=value)


@pytest.mark.parametrize("value", [0, True, 1048577, None])
def test_invalid_byte_budget_never_creates_worker(value):
    from features.policy_write_queue import PolicyWriteQueue
    with pytest.raises(ValueError, match="byte budget"):
        PolicyWriteQueue(max_row_bytes=value)


@pytest.mark.asyncio
async def test_actual_native_parquet_write_keeps_original_and_unknown_t0(tmp_path, monkeypatch):
    from features import store
    from features.policy_write_queue import PolicyWriteQueue
    import pyarrow.parquet as pq
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "_ROW_COUNT", 0)
    t0 = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    queue = PolicyWriteQueue()
    queue.start()
    assert queue.submit({"address": "known", "timestamp": t0})
    assert queue.submit({"address": "unknown", "timestamp": pd.NaT})
    await queue.close()
    rows = pq.read_table(next(tmp_path.glob("features_*.parquet"))).to_pylist()
    assert [row["address"] for row in rows] == ["known", "unknown"]
    assert rows[0]["timestamp"].replace(tzinfo=dt.timezone.utc) == t0
    assert rows[1]["timestamp"] is None
    assert all(row["label"] == 0 and row["sample_type"] == "policy_reject" and row["ts"] is not None for row in rows)
    assert all(row["outcome_trade_id"] is None and row["outcome_execution_proof"] is None for row in rows)
    assert queue.persisted == 2


def native_result(ns):
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                and item.name == "_policy_feature_write_result")
    exec(compile(ast.Module(body=[node], type_ignores=[]), "run_bot.py", "exec"), ns)


@pytest.mark.parametrize("newer", [False, True])
def test_failed_native_result_releases_only_its_original_dedup(newer):
    ns = native_reject(SimpleNamespace(submit=Mock(return_value=True)))
    native_result(ns)
    ns["_store_policy_reject"]({"address": "retry"}, reason="filter")
    original = ns["_policy_reject_seen"]["retry:filter"]
    if newer: ns["_policy_reject_seen"]["retry:filter"] = original + 1
    ns["_policy_feature_write_result"](("retry:filter", original), False, OSError("IO"))
    assert ns["_stats"]["feature_policy_reject_failed"] == 1
    assert ns["_stats"]["feature_policy_reject_pending"] == 0
    assert ("retry:filter" in ns["_policy_reject_seen"]) is newer


def test_native_unmanaged_writer_does_not_start_or_write():
    ns = native_reject(None)
    ns["_store_policy_reject"]({"address": "unmanaged"}, reason="filter")
    ns["store_append"].assert_not_called()
    assert ns["_stats"]["feature_policy_reject_dropped"] == 1 and not ns["_policy_reject_seen"]


@pytest.mark.asyncio
async def test_closed_writer_is_not_restartable_and_cannot_admit():
    from features.policy_write_queue import PolicyWriteQueue
    queue = PolicyWriteQueue(lambda *args, **kw: True)
    await queue.close()
    queue.start()
    with pytest.raises(RuntimeError, match="restarted"): queue.start()
    await queue.close()
    assert not queue.submit({"address": "after-close"})
    assert queue.last_drop_reason == "closed"
    await queue.close()


@pytest.mark.asyncio
async def test_executor_admission_failure_is_reported_and_next_row_can_persist(monkeypatch):
    from features.policy_write_queue import PolicyWriteQueue
    loop = asyncio.get_running_loop()
    execute, calls = loop.run_in_executor, []
    def fail_first(*args):
        calls.append(True)
        if len(calls) == 1: raise OSError("executor admission failed")
        return execute(*args)
    queue = PolicyWriteQueue(lambda *args, **kw: True)
    queue.start()
    with monkeypatch.context() as patch:
        patch.setattr(loop, "run_in_executor", fail_first)
        assert queue.submit({"address": "first"}) and queue.submit({"address": "second"})
        await queue.close()
    assert queue.admitted == 2 and queue.failed == 1 and queue.persisted == 1


@pytest.mark.asyncio
async def test_callback_failure_does_not_erase_actual_commit_or_strand_worker():
    from features.policy_write_queue import PolicyWriteQueue
    def bad_callback(*args): raise ValueError("callback failed")
    queue = PolicyWriteQueue(lambda *args, **kw: True, on_result=bad_callback)
    queue.start()
    assert queue.submit({"address": "first"}) and queue.submit({"address": "second"})
    await queue.close()
    assert queue.persisted == 2 and queue.failed == 0 and queue.callback_errors == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_cleanup", [False, True])
async def test_actual_runner_cannot_publish_stopped_until_policy_worker_drains(monkeypatch, cancel_cleanup):
    import runtime.social_enrichment_queue as socials
    import research_loop.entry_gate_forward as research
    from unittest.mock import AsyncMock
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()
    rows, published = [], []
    def writer(row, *args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        rows.append(row["address"])
        return True
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    async def main(**kwargs):
        kwargs["positions_ready"].set()
        ns["_store_policy_reject"]({"address": "owned-row"}, reason="filter")
        await asyncio.Event().wait()
    async def fault():
        await started.wait()
        raise RuntimeError("synthetic service failure")
    async def forever(*args): await asyncio.Event().wait()
    async def publish():
        assert ns["_runtime_process_state"] == "stopped" and ns["_POLICY_FEATURE_WRITER"] is None
        assert ns["_stats"]["feature_policy_reject_pending"] == 0 and rows == ["owned-row"]
        published.append(True)
    monkeypatch.setattr(socials, "stop_background_tasks", AsyncMock())
    monkeypatch.setattr(research, "stop_background_tasks", AsyncMock())
    ns = native_reject(None)
    ns.update(asyncio=asyncio, DRY_RUN=False, CFG=SimpleNamespace(ML_RETRAIN_IN_MAIN_LOOP=False, HOT_QUEUE_ENABLED=False),
        pumpfun=SimpleNamespace(stop_background_tasks=AsyncMock()), store_append=writer,
        async_init_db=AsyncMock(), SessionLocal=Session, _recover_buy_persistence_outbox=AsyncMock(),
        _recover_close_persistence_outbox=AsyncMock(), _refresh_green_live_risk=AsyncMock(),
        main_loop=main, _dex_discovery_loop=forever, _position_monitor_loop=forever,
        _periodic_labeler=forever, control_command_loop=fault, runtime_state_loop=forever,
        _background_tasks=set(), _publish_runtime_state_once=publish,
        log=SimpleNamespace(info=Mock(), error=Mock(), warning=Mock()))
    native_result(ns)
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == "_runner")
    exec(compile(ast.Module(body=[node], type_ignores=[]), "run_bot.py", "exec"), ns)
    task = asyncio.create_task(ns["_runner"]())
    try:
        await asyncio.wait_for(started.wait(), 2)
        for _ in range(8): await asyncio.sleep(0)
        assert not task.done() and not published
        if cancel_cleanup:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not published
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel_cleanup else RuntimeError):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert published == [True] and ns["_stats"]["feature_policy_reject_persisted"] == 1
