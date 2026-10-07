from __future__ import annotations

import ast
import asyncio
import datetime as dt
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime import loop_scheduler


def _tree():
    return ast.parse(Path("run_bot.py").read_text(encoding="utf-8"), filename="run_bot.py")


def _function(name):
    return next(node for node in _tree().body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)


def test_positions_have_one_supervised_owner_outside_entry_and_discovery():
    main, monitor, runner = (_function(name) for name in ("main_loop", "_position_monitor_loop", "_runner"))
    assert not any(isinstance(node, ast.Name) and node.id == "_check_positions" for node in ast.walk(main))
    assert sum(isinstance(node, ast.Name) and node.id == "_check_positions" for node in ast.walk(monitor)) == 1
    calls = [node for node in ast.walk(runner) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "_position_monitor_loop"]
    assert len(calls) == 1 and calls[0].args[0].id == "positions_ready"
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "supervise"
               for node in ast.walk(runner))
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "set" and isinstance(node.func.value, ast.Name)
               and node.func.value.id == "positions_ready" for node in ast.walk(main))


@pytest.mark.asyncio
async def test_monitor_waits_for_bootstrap_and_runs_while_entry_is_stalled():
    ready, stalled, observed, release = (asyncio.Event() for _ in range(4))
    opened, closed = [], []
    class Session:
        async def __aenter__(self):
            opened.append(self)
            return self
        async def __aexit__(self, *args):
            closed.append(self)
    async def check(session):
        assert stalled.is_set() and not release.is_set()
        observed.set()
    async def slow_entry():
        stalled.set()
        await release.wait()
    monitor = asyncio.create_task(loop_scheduler.monitor_positions(ready=ready, session_factory=Session,
        check=check, interval=lambda: 3, on_success=lambda: None, on_error=lambda exc: pytest.fail(str(exc))))
    entry = asyncio.create_task(slow_entry())
    await asyncio.sleep(0)
    assert not opened
    ready.set()
    await asyncio.wait_for(observed.wait(), timeout=1)
    assert opened == closed and not entry.done()
    monitor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await monitor
    release.set()
    await entry


@pytest.mark.asyncio
async def test_failed_tick_disposes_session_retries_serially_and_does_not_mark_success():
    ready = asyncio.Event()
    ready.set()
    sessions, disposed, errors, successes, sleeps = [], [], [], [], []
    clock = [0.]
    class Session:
        async def __aenter__(self):
            sessions.append(self)
            return self
        async def __aexit__(self, *args):
            disposed.append(self)
    async def check(session):
        assert sessions[:-1] == disposed
        clock[0] += 2
        if len(sessions) == 1:
            raise ValueError("isolated failed transaction")
    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await loop_scheduler.monitor_positions(ready=ready, session_factory=Session, check=check,
            interval=lambda: 1, on_success=lambda: successes.append(True), on_error=errors.append,
            sleep=sleep, clock=lambda: clock[0])
    assert len(sessions) == 2 and sessions == disposed and len(errors) == 1 and successes == [True]
    assert sleeps == [.1, .1]


@pytest.mark.asyncio
async def test_cancelled_monitor_disposes_inflight_session_without_fabricated_health():
    ready, started = asyncio.Event(), asyncio.Event()
    ready.set()
    disposed, successes, errors = [], [], []
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): disposed.append(args[0])
    async def check(session):
        started.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(loop_scheduler.monitor_positions(ready=ready, session_factory=Session,
        check=check, interval=lambda: 1, on_success=lambda: successes.append(True), on_error=errors.append))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert disposed == [asyncio.CancelledError] and not successes and not errors


@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf"), None, "bad"])
def test_invalid_monitor_interval_cannot_busy_loop(value):
    assert loop_scheduler.positive_interval(value) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failure", "ended", "cancelled"])
async def test_supervisor_cancels_and_drains_owned_siblings(mode):
    started, ended = asyncio.Event(), asyncio.Event()
    async def sibling():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            ended.set()
    async def first():
        await started.wait()
        if mode == "failure": raise ValueError("primary failure")
        if mode == "cancelled": raise asyncio.CancelledError
    expected = ValueError if mode == "failure" else asyncio.CancelledError if mode == "cancelled" else RuntimeError
    with pytest.raises(expected):
        await loop_scheduler.supervise([("first", first()), ("sibling", sibling())])
    assert ended.is_set()


@pytest.mark.asyncio
async def test_external_shutdown_drains_supervisor_children():
    started, ended = asyncio.Event(), asyncio.Event()
    async def child():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            ended.set()
    owner = asyncio.create_task(loop_scheduler.supervise([("child", child())]))
    await started.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError): await owner
    assert ended.is_set()


@pytest.mark.asyncio
async def test_actual_monitor_wrapper_updates_health_only_after_completed_tick():
    ready = asyncio.Event()
    ready.set()
    calls, disposed = [], []
    now = dt.datetime(2026, 10, 7, tzinfo=dt.timezone.utc)
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): disposed.append(self)
    async def check(session):
        calls.append(session)
        raise asyncio.CancelledError
    namespace = {"asyncio": asyncio, "SessionLocal": Session, "_check_positions": check,
        "runner_turbo_monitor": SimpleNamespace(target_sleep_seconds=lambda *args, **kw: 1),
        "SLEEP_SECONDS": 3, "DRY_RUN": True, "utc_now": lambda: now, "_last_monitor_ok_at": None,
        "_note_runtime_error": lambda *args: pytest.fail(str(args)), "log": SimpleNamespace(error=lambda *args: None)}
    exec(compile(ast.Module(body=[_function("_position_monitor_loop")], type_ignores=[]), "run_bot.py", "exec"), namespace)
    with pytest.raises(asyncio.CancelledError): await namespace["_position_monitor_loop"](ready)
    assert len(calls) == 1 and calls == disposed and namespace["_last_monitor_ok_at"] is None


@pytest.mark.asyncio
async def test_real_prefetch_bypasses_ok_and_nil_caches_without_network():
    calls = []
    async def get_many(addresses, **kwargs):
        calls.append((addresses, kwargs))
        return {addresses[0]: 2}
    namespace = {"List": list, "Dict": dict, "USE_JUPITER_PRICE": True, "math": math,
        "jupiter_price": SimpleNamespace(get_many_usd_prices=get_many),
        "log": SimpleNamespace(warning=lambda *args: None, debug=lambda *args: None)}
    exec(compile(ast.Module(body=[_function("_prefetch_batch_prices")], type_ignores=[]), "run_bot.py", "exec"), namespace)
    mint = "A" * 32
    assert await namespace["_prefetch_batch_prices"]([mint]) == {mint: 2}
    assert calls == [([mint], {"force_refresh": True})]


@pytest.mark.asyncio
async def test_prefetch_rejects_nonfinite_zero_boolean_and_out_of_request_prices():
    async def get_many(addresses, **kwargs):
        return {"A" * 32: float("nan"), "B" * 32: float("inf"), "C" * 32: 0,
                "D" * 32: True, "E" * 32: -1, "F" * 32: 3, "G" * 32: 8}
    namespace = {"List": list, "Dict": dict, "USE_JUPITER_PRICE": True, "math": math,
        "jupiter_price": SimpleNamespace(get_many_usd_prices=get_many),
        "log": SimpleNamespace(warning=lambda *args: None, debug=lambda *args: None)}
    exec(compile(ast.Module(body=[_function("_prefetch_batch_prices")], type_ignores=[]), "run_bot.py", "exec"), namespace)
    assert await namespace["_prefetch_batch_prices"]([letter * 32 for letter in "ABCDEF"]) == {"F" * 32: 3}


@pytest.mark.asyncio
async def test_owned_entry_sessions_rollback_exception_timeout_and_allow_next_commit(tmp_path):
    from runtime.buy_recovery import BuyRecoveryStore
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'isolated.db').as_posix()}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.exec_driver_sql("CREATE TABLE entry_fixture (id INTEGER PRIMARY KEY)")
    calls, errors = [], []
    async def evaluate(token, session):
        calls.append(session)
        if token["mode"] == "failed":
            await session.execute(text("INSERT INTO entry_fixture VALUES (1)"))
            raise ValueError("synthetic entry failure")
        if token["mode"] == "timeout":
            await session.execute(text("INSERT INTO entry_fixture VALUES (3)"))
            await asyncio.Event().wait()
        assert await session.scalar(text("SELECT COUNT(*) FROM entry_fixture")) == 0
        await session.execute(text("INSERT INTO entry_fixture VALUES (2)"))
        await session.commit()
    namespace = {"asyncio": asyncio, "CFG": SimpleNamespace(DRY_RUN=True), "PROJECT_ROOT": tmp_path,
        "SessionLocal": sessions, "EVALUATE_TOKEN_TIMEOUT_S": .1, "_evaluate_and_buy": evaluate,
        "_BUY_RECOVERY": BuyRecoveryStore(tmp_path / "buy_journal"),
        "_note_runtime_error": lambda *args: errors.append(args), "log": SimpleNamespace(error=lambda *args: None)}
    exec(compile(ast.Module(body=[_function("_evaluate_and_buy_guarded")], type_ignores=[]), "run_bot.py", "exec"), namespace)
    try:
        for mode in ("failed", "timeout", "ok"):
            await namespace["_evaluate_and_buy_guarded"]({"address": "synthetic", "mode": mode}, None, source="test")
        assert len({id(session) for session in calls}) == 3 and len(errors) == 2
        async with sessions() as session:
            assert (await session.execute(text("SELECT id FROM entry_fixture"))).scalars().all() == [2]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_runner_drains_owned_loops_and_background_before_stopped_publication(tmp_path, monkeypatch):
    import runtime.social_enrichment_queue as socials
    import research_loop.entry_gate_forward as research
    monitor_started, background_started = asyncio.Event(), asyncio.Event()
    cleaned, published = set(), []
    async def init(): pass
    class RecoverySession:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): cleaned.add("recovery")
    async def recovery(session, **kwargs): assert kwargs["force"]
    async def buy_recovery(session, **kwargs): assert kwargs["force"]
    async def archive_recovery(**kwargs):
        assert kwargs["force"]
        cleaned.add("archive_recovery")
    async def main(*, positions_ready):
        try:
            positions_ready.set()
            await asyncio.Event().wait()
        finally: cleaned.add("main")
    async def monitor(ready):
        try:
            await ready.wait()
            monitor_started.set()
            await asyncio.Event().wait()
        finally: cleaned.add("monitor")
    async def loop(name):
        try: await asyncio.Event().wait()
        finally: cleaned.add(name)
    async def fault():
        await monitor_started.wait()
        await background_started.wait()
        raise ValueError("synthetic control-loop failure")
    async def background():
        try:
            background_started.set()
            await asyncio.Event().wait()
        finally: cleaned.add("background")
    task = asyncio.create_task(background())
    async def stop_social(): cleaned.add("social")
    async def stop_research(): cleaned.add("research")
    from runtime import trade_learning
    async def export_loop(**kwargs):
        await kwargs["ready"].wait()
        await loop("trade_export")
    monkeypatch.setattr(trade_learning, "run_export_loop", export_loop)
    monkeypatch.setattr(socials, "stop_background_tasks", stop_social)
    monkeypatch.setattr(research, "stop_background_tasks", stop_research)
    async def publish():
        assert namespace["_runtime_process_state"] == "stopped"
        assert cleaned == {"recovery", "archive_recovery", "main", "monitor", "labeler", "state", "background", "social", "research", "trade_export"}
        published.append(True)
    namespace = {"asyncio": asyncio, "CFG": SimpleNamespace(ML_RETRAIN_IN_MAIN_LOOP=False),
        "DRY_RUN": True, "_repair_paper_archive_evidence": archive_recovery, "PROJECT_ROOT": tmp_path,
        "_stats": {"appended_at_close": 0},
        "async_init_db": init, "SessionLocal": RecoverySession, "_recover_close_persistence_outbox": recovery,
        "_recover_buy_persistence_outbox": buy_recovery,
        "main_loop": main, "_position_monitor_loop": monitor, "_periodic_labeler": lambda: loop("labeler"),
        "runtime_state_loop": lambda: loop("state"), "control_command_loop": fault,
        "_background_tasks": {task}, "_publish_runtime_state_once": publish,
        "_note_runtime_error": lambda *args: None,
        "log": SimpleNamespace(info=lambda *args: None, error=lambda *args: None)}
    exec(compile(ast.Module(body=[_function("_runner")], type_ignores=[]), "run_bot.py", "exec"), namespace)
    with pytest.raises(ValueError, match="synthetic control-loop failure"):
        await namespace["_runner"]()
    assert task.done() and published == [True]
