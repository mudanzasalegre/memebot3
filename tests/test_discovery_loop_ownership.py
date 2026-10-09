"""Native discovery/entry scheduling with isolated clocks, queues and feeds."""
from __future__ import annotations

import ast
import asyncio
from collections import deque
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from fetcher import pumpfun
from runtime import loop_scheduler


def function(name):
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"), filename="run_bot.py")
    return next(node for node in tree.body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == name)


def native_entry_loop(namespace):
    # Execute the original service loop, not startup, signing or operator I/O.
    main = function("main_loop")
    loop = next(node for node in main.body if isinstance(node, ast.While))
    globals_ = [node for node in main.body if isinstance(node, ast.Global)]
    # The pre-repair loop had a local cadence clock; retain that original
    # initializer when running the same regression against its source.
    cadence = [node for node in main.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "last_discovery"
                for target in node.targets)]
    wrapper = ast.AsyncFunctionDef(name="entry_loop", args=ast.arguments(
        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[*globals_, *cadence, loop], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    exec(compile(module, "run_bot.py", "exec"), namespace)
    return namespace["entry_loop"]


def compile_owners(namespace):
    module = ast.Module(body=[function(name) for name in (
        "_dex_discovery_loop", "_pump_discovery_loop")], type_ignores=[])
    exec(compile(module, "run_bot.py", "exec"), namespace)
    return namespace["_dex_discovery_loop"], namespace["_pump_discovery_loop"]


def entry_namespace(*, queue, evaluate, fetch):
    async def noop(*args, **kwargs):
        return None
    return {
        "time": SimpleNamespace(monotonic=lambda: 100.), "last_discovery": 0.,
        "_refresh_balance": noop, "DISCOVERY_INTERVAL": 45,
        "_runtime_discovery_paused": False, "fetch_candidate_pairs": fetch,
        "_queue_add_if_new": lambda address: None, "utc_now": lambda: None,
        "_last_discovery_ok_at": None, "pumpfun": SimpleNamespace(get_latest_pumpfun=noop),
        "CFG": SimpleNamespace(HOT_QUEUE_ENABLED=True, HOT_QUEUE_BATCH_SIZE=12),
        "hot_queue_enabled": True, "GLOBAL_HOT_QUEUE": queue,
        "_evaluate_and_buy_guarded": evaluate, "SLEEP_SECONDS": 3,
        "next_ready_pair": lambda: None, "VALIDATION_BATCH_SIZE": 30,
        "DRY_RUN": False, "REAL_SHADOW_SIM": False, "_shadow_positions": {},
        "_last_stats_print": 100., "_last_csv_export": 100.,
        "_maybe_regenerate_core_reports": noop, "asyncio": asyncio,
        "_note_runtime_error": lambda *args: pytest.fail(str(args)),
        "log": SimpleNamespace(error=lambda *args: None),
    }


@pytest.mark.asyncio
async def test_existing_hot_candidate_is_not_held_by_dex_pull():
    evaluated, dex_started = asyncio.Event(), asyncio.Event()
    incoming = [{"address": "original-opportunity", "created_at": "original-clock"}]
    class Queue:
        def pop_batch(self, limit, *, expand):
            assert limit == 1 and expand is False
            return [incoming.pop(0)] if incoming else []
    async def fetch():
        dex_started.set()
        await asyncio.Event().wait()
    async def evaluate(token, session, *, source):
        assert token["created_at"] == "original-clock"
        assert session is None and source == "hot_queue"
        evaluated.set()
    task = asyncio.create_task(native_entry_loop(entry_namespace(
        queue=Queue(), evaluate=evaluate, fetch=fetch))())
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        assert evaluated.is_set(), "A stalled discovery request held an already queued opportunity"
        assert not dex_started.is_set(), "Entry service must not own the Dex provider pull"
    finally:
        task.cancel()
        result, = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result, asyncio.CancelledError), result


@pytest.mark.asyncio
async def test_native_pump_admission_continues_while_dex_and_single_entry_are_stalled(monkeypatch):
    ready, dex_started, entry_started, admitted_peer = (asyncio.Event() for _ in range(4))
    disposed, calls, active, peak = [], [], [0], [0]
    pending = deque()
    original = [
        {"address": "A", "created_at": "original-birth-A", "discovered_at": "original-receipt-A"},
        {"address": "B", "created_at": "original-birth-B", "discovered_at": "original-receipt-B"},
    ]
    class Queue:
        def add_many(self, tokens, *, source):
            assert source == "pumpfun"
            pending.extend(deepcopy(tokens))
            if any(token["address"] == "B" for token in tokens):
                admitted_peer.set()
            return [True] * len(tokens)
        def pop_batch(self, limit, *, expand):
            return [pending.popleft()] if pending else []
    async def fetch_dex():
        try:
            dex_started.set()
            await asyncio.Event().wait()
        finally:
            disposed.append("dex")
    async def fetch_pump():
        index = len(calls)
        calls.append(index)
        if index == 1:
            await entry_started.wait()
        if index < 2:
            return [original[index]]
        await asyncio.Event().wait()
    async def evaluate(token, session, *, source):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        try:
            assert token == original[0] and source == "hot_queue" and session is None
            entry_started.set()
            await asyncio.Event().wait()
        finally:
            active[0] -= 1
            disposed.append("entry")
    poll = loop_scheduler.poll_discovery
    async def fast_poll(**kwargs):
        assert kwargs["interval"]() in (3, 45)
        return await poll(**kwargs, sleep=lambda delay: asyncio.sleep(0))
    monkeypatch.setattr(loop_scheduler, "poll_discovery", fast_poll)
    queue = Queue()
    namespace = entry_namespace(queue=queue, evaluate=evaluate, fetch=fetch_dex)
    namespace["pumpfun"] = SimpleNamespace(get_latest_pumpfun=fetch_pump)
    # Cadence is a controlled clock here, not a one-second production SLA.
    namespace["asyncio"] = SimpleNamespace(Event=asyncio.Event,
        sleep=lambda delay: asyncio.sleep(0))
    dex, pump = compile_owners(namespace)
    tasks = [asyncio.create_task(dex(ready)), asyncio.create_task(pump(ready)),
             asyncio.create_task(native_entry_loop(namespace)())]
    try:
        await asyncio.sleep(0)
        assert not calls and not dex_started.is_set()
        ready.set()
        await asyncio.wait_for(admitted_peer.wait(), timeout=1)
        assert dex_started.is_set() and entry_started.is_set()
        assert list(pending) == [original[1]] and peak == [1] and active == [1]
        assert not namespace["_last_discovery_ok_at"]
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results), results
    assert set(disposed) == {"dex", "entry"} and active == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize("feed", ["dex", "pump"])
async def test_native_owner_obeys_readiness_pause_and_resumes_without_new_task(monkeypatch, feed):
    ready, observed = asyncio.Event(), asyncio.Event()
    calls, queued, health, sleeps = [], [], [], []
    namespace = {"asyncio": asyncio, "_runtime_discovery_paused": True,
        "DISCOVERY_INTERVAL": 45, "SLEEP_SECONDS": 3, "_last_discovery_ok_at": None,
        "utc_now": lambda: "original-completion", "_queue_add_if_new": queued.append,
        "_note_runtime_error": lambda *args: pytest.fail(str(args)),
        "log": SimpleNamespace(error=lambda *args: None)}
    class Queue:
        def add_many(self, tokens, *, source):
            queued.extend(tokens)
            return [True] * len(tokens)
    async def fetch():
        calls.append(feed)
        observed.set()
        return ["D"] if feed == "dex" else [{"address": "P", "discovered_at": "original"}]
    namespace["fetch_candidate_pairs"] = fetch
    namespace["pumpfun"] = SimpleNamespace(get_latest_pumpfun=fetch)
    namespace["GLOBAL_HOT_QUEUE"] = Queue()
    poll = loop_scheduler.poll_discovery
    async def sleep(delay):
        sleeps.append(delay)
        if not calls:
            namespace["_runtime_discovery_paused"] = False
            await asyncio.sleep(0)
        else:
            await asyncio.Event().wait()
    async def controlled(**kwargs):
        return await poll(**kwargs, sleep=sleep, clock=lambda: 100.)
    monkeypatch.setattr(loop_scheduler, "poll_discovery", controlled)
    owners = compile_owners(namespace)
    task = asyncio.create_task(owners[feed == "pump"](ready))
    try:
        await asyncio.sleep(0)
        assert not calls and not sleeps
        ready.set()
        await asyncio.wait_for(observed.wait(), timeout=1)
        assert calls == [feed] and len(queued) == 1
        assert sleeps[0] == (45 if feed == "dex" else 3)
        assert namespace["_last_discovery_ok_at"] == ("original-completion" if feed == "dex" else None)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_poll_owner_is_serial_retries_error_without_success_or_catchup_burst():
    ready = asyncio.Event()
    ready.set()
    now, active, peak, calls, errors, sleeps = [0.], [0], [0], [], [], []
    async def tick():
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        try:
            calls.append(now[0])
            now[0] += 12
            if len(calls) == 1:
                raise ValueError("isolated feed failure")
        finally:
            active[0] -= 1
    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
        if len(sleeps) == 2:
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await loop_scheduler.poll_discovery(ready=ready, tick=tick, interval=lambda: 3,
            enabled=lambda: True, on_error=errors.append, sleep=sleep, clock=lambda: now[0])
    assert calls == [0., 12.1] and peak == [1] and active == [0]
    assert sleeps == [.1, .1] and len(errors) == 1 and str(errors[0]) == "isolated feed failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("period", [True, False, 0, -1, float("nan"), float("inf"), None, "bad"])
async def test_invalid_discovery_cadence_cannot_busy_loop(period):
    ready = asyncio.Event()
    ready.set()
    sleeps = []
    async def tick():
        return None
    async def sleep(delay):
        sleeps.append(delay)
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await loop_scheduler.poll_discovery(ready=ready, tick=tick, interval=lambda: period,
            enabled=lambda: True, on_error=lambda exc: pytest.fail(str(exc)),
            sleep=sleep, clock=lambda: 1.)
    assert sleeps == [3.]


def test_native_runner_owns_one_feed_consumer_and_freezes_hot_mode():
    runner, main = function("_runner"), function("main_loop")
    for name in ("_dex_discovery_loop", "_pump_discovery_loop", "main_loop"):
        calls = [node for node in ast.walk(runner) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == name]
        assert len(calls) == 1
    main_call = next(node for node in ast.walk(runner) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "main_loop")
    assert any(keyword.arg == "hot_queue_enabled" and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "hot_queue_enabled" for keyword in main_call.keywords)
    service = next(node for node in main.body if isinstance(node, ast.While))
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "fetch_candidate_pairs" for node in ast.walk(service))
    assert not any(isinstance(node, ast.Attribute) and node.attr == "HOT_QUEUE_ENABLED"
        for node in ast.walk(service))


@pytest.fixture
def isolated_socket(monkeypatch):
    monkeypatch.setattr(pumpfun, "_ws_task", None)
    monkeypatch.setattr(pumpfun, "_ws_lock", asyncio.Lock())
    monkeypatch.setattr(pumpfun, "_started", asyncio.Event())
    monkeypatch.setattr(pumpfun, "_buffer", deque([{"address": "pending-original"}]))
    monkeypatch.setattr(pumpfun, "_pending", {"pending-original"})
    monkeypatch.setattr(pumpfun, "_seen", {"delivered-original": "original-receipt"})


@pytest.mark.asyncio
async def test_socket_shutdown_drains_before_return_and_preserves_original_buffer(isolated_socket):
    started, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def socket():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()
    task = asyncio.create_task(socket())
    pumpfun._ws_task = task
    pumpfun._started.set()
    await started.wait()
    stop = asyncio.create_task(pumpfun.stop_background_tasks())
    await cleanup.wait()
    assert not stop.done() and pumpfun._ws_task is task
    release.set()
    await stop
    assert task.done() and pumpfun._ws_task is None and not pumpfun._started.is_set()
    assert list(pumpfun._buffer) == [{"address": "pending-original"}]
    assert pumpfun._pending == {"pending-original"}
    assert pumpfun._seen == {"delivered-original": "original-receipt"}
    await pumpfun.stop_background_tasks()


@pytest.mark.asyncio
async def test_concurrent_shutdowns_cancel_one_socket_and_allow_explicit_restart(isolated_socket, monkeypatch):
    starts, cleanups = [], []
    async def socket():
        starts.append(True)
        pumpfun._started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanups.append(True)
            await asyncio.sleep(0)
    monkeypatch.setattr(pumpfun, "_ws_consumer", socket)
    monkeypatch.setattr(pumpfun, "_WS_DISABLED_REASON", "")
    try:
        await pumpfun._ensure_started()
        first = pumpfun._ws_task
        await asyncio.gather(pumpfun.stop_background_tasks(), pumpfun.stop_background_tasks())
        assert first.done() and pumpfun._ws_task is None and len(starts) == len(cleanups) == 1
        await pumpfun._ensure_started()
        assert pumpfun._ws_task is not first and starts == [True, True]
    finally:
        await pumpfun.stop_background_tasks()
    assert len(cleanups) == 2


@pytest.mark.asyncio
async def test_repeated_shutdown_cancellation_does_not_interrupt_socket_cleanup(isolated_socket):
    started, cleanup, release, disposed = (asyncio.Event() for _ in range(4))
    async def socket():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()
            disposed.set()
    task = asyncio.create_task(socket())
    pumpfun._ws_task = task
    await started.wait()
    stop = asyncio.create_task(pumpfun.stop_background_tasks())
    await cleanup.wait()
    for _ in range(3):
        stop.cancel()
        await asyncio.sleep(0)
        assert not stop.done() and not task.done() and not disposed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stop
    assert disposed.is_set() and task.done() and pumpfun._ws_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("feed", ["dex", "pump"])
async def test_native_cancelled_pull_keeps_no_false_success_and_releases_owner(feed, monkeypatch):
    ready, started, disposed = (asyncio.Event() for _ in range(3))
    ready.set()
    errors, queued = [], []
    async def fetch():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            disposed.set()
    namespace = {"asyncio": asyncio, "_runtime_discovery_paused": False,
        "DISCOVERY_INTERVAL": 45, "SLEEP_SECONDS": 3, "_last_discovery_ok_at": None,
        "fetch_candidate_pairs": fetch, "pumpfun": SimpleNamespace(get_latest_pumpfun=fetch),
        "GLOBAL_HOT_QUEUE": SimpleNamespace(add_many=lambda rows, **kw: queued.extend(rows)),
        "_queue_add_if_new": queued.append, "utc_now": lambda: "not-completed",
        "_note_runtime_error": lambda *args: errors.append(args),
        "log": SimpleNamespace(error=lambda *args: None)}
    owners = compile_owners(namespace)
    task = asyncio.create_task(owners[feed == "pump"](ready))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert disposed.is_set() and not queued and not errors
    assert namespace["_last_discovery_ok_at"] is None
