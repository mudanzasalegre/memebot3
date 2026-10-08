"""Actual GMGN/lifecycle consumers, synthetic signer/RPC and temporary journals."""
from __future__ import annotations

import ast
import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from runtime import owned_dispatch as owned
from runtime.loop_scheduler import monitor_positions
from runtime.buy_recovery import BuyRecoveryError, BuyRecoveryStore
from runtime.sell_recovery import SellRecoveryError, SellRecoveryStore
from trader import buyer, gmgn, seller
from test_buy_recovery import prototype
from test_sell_recovery import position, namespace as sell_namespace


@pytest.fixture
def fake_signer(monkeypatch):
    import trader
    fake = SimpleNamespace(PUBLIC_KEY="synthetic-owner", sign_and_send=Mock())
    # Never ask the lazy package getter to initialize the operator signer.
    monkeypatch.setitem(trader.__dict__, "sol_signer", fake)
    monkeypatch.setattr(gmgn, "_route", AsyncMock(return_value={
        "data": {"raw_tx": {"swapTransaction": "synthetic-packet"}}}))
    return fake


def blocking_signer(fake, *, error=None, context=None):
    loop = asyncio.get_running_loop()
    started, release, observations = asyncio.Event(), threading.Event(), []
    def send(packet):
        observations.append((packet, threading.get_ident(), context.get() if context else None))
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise RuntimeError("Synthetic worker was not released")
        if error is not None:
            raise error
        return "synthetic-signature"
    fake.sign_and_send.side_effect = send
    return started, release, observations


def live_guards(monkeypatch, operation):
    if operation == "buy":
        monkeypatch.setattr(buyer, "_JUP_ROUTER_AVAILABLE", False)
        monkeypatch.setattr(buyer, "_REQUIRE_JUP_PRICE", False)
        monkeypatch.setattr(buyer, "is_in_trading_window", lambda: True)
        monkeypatch.setattr(buyer, "_max_positions_reached", AsyncMock(return_value=False))
        monkeypatch.setattr(buyer, "_has_enough_funds", AsyncMock(return_value=True))
        return buyer
    monkeypatch.setattr(seller, "_JUP_ROUTER_AVAILABLE", False)
    return seller


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["buy", "sell"])
async def test_actual_gmgn_worker_keeps_real_monitor_serial_and_responsive(fake_signer, operation):
    context = contextvars.ContextVar("synthetic-owner", default=None)
    context.set("original-entry-owner")
    started, release, observations = blocking_signer(fake_signer, context=context)
    ticks, disposed, observed = [], [], asyncio.Event()
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): disposed.append(self)
    async def check(session):
        ticks.append(session)
        if len(ticks) >= 3: observed.set()
    ready = asyncio.Event()
    ready.set()
    monitor = asyncio.create_task(monitor_positions(ready=ready, session_factory=Session,
        check=check, interval=lambda: .1, on_success=lambda: None,
        on_error=lambda exc: pytest.fail(str(exc)), sleep=lambda _: asyncio.sleep(0)))
    task = asyncio.create_task(getattr(gmgn, operation)("synthetic-mint", .1 if operation == "buy" else 2**53 + 1))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(observed.wait(), 2)
        assert not task.done() and owned.pending_dispatch_count() == 1
        assert observations == [("synthetic-packet", observations[0][1], "original-entry-owner")]
        assert observations[0][1] != threading.get_ident()
        release.set()
        result = await task
        assert result["signature"] == "synthetic-signature"
        gmgn._route.assert_awaited_once()
        assert gmgn._route.await_args.args[2] == (100_000_000 if operation == "buy" else 2**53 + 1)
        fake_signer.sign_and_send.assert_called_once()
    finally:
        release.set()
        monitor.cancel()
        await asyncio.gather(monitor, task, return_exceptions=True)
    assert ticks == disposed and owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["buy", "sell"])
@pytest.mark.parametrize("error", [None, TimeoutError("synthetic acknowledgement loss")])
async def test_repeated_cancel_keeps_actual_journal_owner_until_worker_settles(tmp_path, fake_signer, monkeypatch, operation, error):
    started, release, _ = blocking_signer(fake_signer, error=error)
    venue = live_guards(monkeypatch, operation)
    if operation == "buy":
        store = BuyRecoveryStore(tmp_path / "buy")
        async def execute():
            with store.scope():
                store.begin(prototype(paper=False), paper=False, amount_sol=.1)
                await venue.buy("A" * 32, .1)
        active = lambda: store._active
    else:
        store = SellRecoveryStore(tmp_path / "sell")
        ns = sell_namespace(tmp_path, store, seller=venue, paper_mode=False)
        async def execute():
            await ns["_sell_position_guarded"](position(paper_mode=False), 400,
                reason="synthetic-exit", liquidity_usd=1_000_000_000.)
        active = lambda: store.active
    task = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(started.wait(), 2)
        original = next(iter(active()))
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and active() == {original}
            assert not store.pending_addresses and owned.pending_dispatch_count() == 1
        # A separately loaded journal cannot misclassify the unknown live result.
        assert type(store)(store.directory).pending_addresses == {"A" * 32}
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert not active() and store.pending_addresses == {"A" * 32}
    rows = store._records if operation == "buy" else store.records
    assert rows[original]["state"] == "prepared" and "fill" not in rows[original]
    assert owned.pending_dispatch_count() == 0
    gmgn._route.assert_awaited_once()
    fake_signer.sign_and_send.assert_called_once()
    if operation == "buy":
        with store.scope(), pytest.raises(BuyRecoveryError):
            store.begin(prototype(paper=False), paper=False, amount_sol=.1)
    else:
        with pytest.raises(SellRecoveryError):
            store.begin(position(paper_mode=False), 400, paper=False, reason="retry")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["buy", "sell"])
async def test_actual_live_consumer_keeps_worker_error_ambiguous_without_retry(fake_signer, monkeypatch, operation):
    from runtime.buy_recovery import BuyOutcomeUncertain
    from runtime.sell_recovery import SellOutcomeUncertain
    venue = live_guards(monkeypatch, operation)
    failure = TimeoutError("synthetic possibly submitted RPC")
    fake_signer.sign_and_send.side_effect = failure
    try:
        if operation == "buy":
            with pytest.raises(BuyOutcomeUncertain) as caught: await venue.buy("A" * 32, .1)
        else:
            with pytest.raises(SellOutcomeUncertain) as caught:
                await venue.sell("A" * 32, 400, liquidity_usd=1_000_000_000.)
        assert caught.value.__cause__ is failure
    finally:
        await owned.drain_dispatch()
    fake_signer.sign_and_send.assert_called_once()
    gmgn._route.assert_awaited_once()
    assert owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
async def test_actual_seller_dispatches_canonical_mint_not_address_alias(fake_signer, monkeypatch):
    live_guards(monkeypatch, "sell")
    fake_signer.sign_and_send.return_value = "synthetic-signature"
    monkeypatch.setattr(seller, "_resolve_close_price_usd", AsyncMock(return_value=(2., "synthetic")))
    response = await seller.sell("B" * 32, 2**53 + 1, token_mint="A" * 32, liquidity_usd=1_000_000_000.)
    assert response["signature"] == "synthetic-signature"
    assert gmgn._route.await_args.args[:3] == ("A" * 32, gmgn.SOL_MINT, 2**53 + 1)
    assert owned.pending_dispatch_count() == 0
    fake_signer.sign_and_send.assert_called_once()


@pytest.mark.asyncio
async def test_wait_for_does_not_finish_timeout_before_irreversible_worker(fake_signer):
    started, release, _ = blocking_signer(fake_signer)
    task = asyncio.create_task(asyncio.wait_for(gmgn.buy("synthetic-mint", .1), .02))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.sleep(.05)
        assert not task.done() and owned.pending_dispatch_count() == 1
        release.set()
        with pytest.raises(asyncio.TimeoutError): await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    fake_signer.sign_and_send.assert_called_once()
    gmgn._route.assert_awaited_once()
    assert owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("synthetic bad packet"), TimeoutError("synthetic RPC"), asyncio.CancelledError()])
async def test_original_worker_exception_is_not_retried_or_lost(error):
    function = Mock(side_effect=error)
    with pytest.raises(type(error)) as caught:
        await owned.run_owned_sync(function)
    assert caught.value is error and owned.pending_dispatch_count() == 0
    function.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, {"signature": "synthetic"}])
async def test_worker_preserves_result_and_keyword_arguments(value):
    function = Mock(return_value=value)
    assert await owned.run_owned_sync(function, "original", amount=100_000_000) is value
    function.assert_called_once_with("original", amount=100_000_000)
    assert owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
async def test_executor_rejection_never_creates_phantom_owner_or_calls_function(monkeypatch):
    function, failure = Mock(), RuntimeError("synthetic executor stopped")
    monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", Mock(side_effect=failure))
    with pytest.raises(RuntimeError) as caught: await owned.run_owned_sync(function)
    assert caught.value is failure and owned.pending_dispatch_count() == 0
    function.assert_not_called()


@pytest.mark.asyncio
async def test_interrupted_await_cannot_abandon_unfinished_execution_thread(monkeypatch, fake_signer):
    class SyntheticInterrupt(BaseException): pass
    interruption = SyntheticInterrupt("synthetic interrupted await")
    started, release, _ = blocking_signer(fake_signer)
    loop, shield, calls = asyncio.get_running_loop(), asyncio.shield, []
    def interrupt_once(future):
        calls.append(True)
        if len(calls) == 1:
            async def interrupt_running_worker():
                await started.wait()
                raise interruption
            return loop.create_task(interrupt_running_worker())
        return shield(future)
    monkeypatch.setattr(asyncio, "shield", interrupt_once)
    task = asyncio.create_task(gmgn.buy("synthetic-mint", .1))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert not task.done() and owned.pending_dispatch_count() == 1
        release.set()
        with pytest.raises(SyntheticInterrupt) as caught: await task
        assert caught.value is interruption
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    fake_signer.sign_and_send.assert_called_once()
    assert owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
async def test_cancel_before_dispatch_never_submits_work():
    function = Mock()
    async def execute():
        asyncio.current_task().cancel()
        await owned.run_owned_sync(function)
    with pytest.raises(asyncio.CancelledError): await asyncio.create_task(execute())
    function.assert_not_called()
    assert owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
async def test_cancel_during_route_never_loads_execution_worker(fake_signer, monkeypatch):
    started = asyncio.Event()
    async def route(*args):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(gmgn, "_route", AsyncMock(side_effect=route))
    task = asyncio.create_task(gmgn.buy("synthetic-mint", .1))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    fake_signer.sign_and_send.assert_not_called()
    assert owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
async def test_cancelled_queued_worker_is_never_invoked(monkeypatch):
    loop, release = asyncio.get_running_loop(), threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    occupied = executor.submit(release.wait, 5)
    original = loop.run_in_executor
    monkeypatch.setattr(loop, "run_in_executor", lambda _, fn, *args: original(executor, fn, *args))
    function = Mock()
    task = asyncio.create_task(owned.run_owned_sync(function))
    try:
        while owned.pending_dispatch_count() != 1: await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        occupied.result(timeout=2)
        executor.shutdown(wait=True)
    function.assert_not_called()
    assert owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
async def test_closing_drain_joins_all_workers_despite_repeated_cancellation():
    loop = asyncio.get_running_loop()
    started = [asyncio.Event(), asyncio.Event()]
    release = [threading.Event(), threading.Event()]
    def execute(index):
        loop.call_soon_threadsafe(started[index].set)
        assert release[index].wait(5)
        return index
    tasks = [asyncio.create_task(owned.run_owned_sync(execute, index)) for index in range(2)]
    await asyncio.gather(*(event.wait() for event in started))
    with pytest.raises(RuntimeError, match="earlier runtime"): owned.open_dispatch()
    drain = asyncio.create_task(owned.drain_dispatch())
    try:
        for _ in range(3):
            await asyncio.sleep(0)
            drain.cancel()
            await asyncio.sleep(0)
            assert not drain.done()
        forbidden = Mock()
        with pytest.raises(RuntimeError, match="closed"):
            await owned.run_owned_sync(forbidden)
        forbidden.assert_not_called()
        release[0].set()
        assert await tasks[0] == 0 and not drain.done()
        release[1].set()
        assert await tasks[1] == 1
        with pytest.raises(asyncio.CancelledError): await drain
    finally:
        for event in release: event.set()
        await asyncio.gather(*tasks, drain, return_exceptions=True)
    assert owned.pending_dispatch_count() == 0
    owned.open_dispatch()
    assert await owned.run_owned_sync(lambda: "new-generation") == "new-generation"


@pytest.mark.asyncio
async def test_actual_runner_cannot_publish_stopped_while_execution_thread_is_live(monkeypatch):
    import runtime.social_enrichment_queue as socials
    import research_loop.entry_gate_forward as research
    loop = asyncio.get_running_loop()
    started, release, published, cleaned = asyncio.Event(), threading.Event(), [], set()
    def worker():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    async def main(*, positions_ready):
        positions_ready.set()
        try: await owned.run_owned_sync(worker)
        finally: cleaned.add("entry")
    async def monitor(ready):
        await ready.wait()
        try: await asyncio.Event().wait()
        finally: cleaned.add("monitor")
    async def fault():
        await started.wait()
        raise RuntimeError("synthetic supervised failure")
    async def forever(): await asyncio.Event().wait()
    async def publish():
        assert owned.pending_dispatch_count() == 0 and cleaned == {"entry", "monitor"}
        assert ns["_runtime_process_state"] == "stopped"
        published.append(True)
    monkeypatch.setattr(socials, "stop_background_tasks", AsyncMock())
    monkeypatch.setattr(research, "stop_background_tasks", AsyncMock())
    ns = {"asyncio": asyncio, "DRY_RUN": False, "CFG": SimpleNamespace(ML_RETRAIN_IN_MAIN_LOOP=False),
        "async_init_db": AsyncMock(), "SessionLocal": Session,
        "_recover_buy_persistence_outbox": AsyncMock(), "_recover_close_persistence_outbox": AsyncMock(),
        "_refresh_green_live_risk": AsyncMock(return_value=True),
        "main_loop": main, "_position_monitor_loop": monitor, "control_command_loop": fault,
        "_periodic_labeler": forever, "runtime_state_loop": forever, "_background_tasks": set(),
        "_publish_runtime_state_once": publish, "_note_runtime_error": Mock(),
        "log": SimpleNamespace(info=Mock(), error=Mock())}
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_runner")
    exec(compile(ast.Module(body=[node], type_ignores=[]), "run_bot.py", "exec"), ns)
    task = asyncio.create_task(ns["_runner"]())
    try:
        await asyncio.wait_for(started.wait(), 2)
        for _ in range(4): await asyncio.sleep(0)
        assert not task.done() and not published and owned.pending_dispatch_count() == 1
        release.set()
        with pytest.raises(RuntimeError, match="synthetic supervised failure"): await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert published == [True] and owned.pending_dispatch_count() == 0
