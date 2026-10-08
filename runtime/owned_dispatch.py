"""Non-blocking ownership of irreversible synchronous execution work.

Cancellation is not proof that a thread stopped or that an order was absent.
Keep the caller (and its durable intent/position scope) alive until the worker
settles, then propagate the original cancellation. Never retry the callable.
"""
from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from functools import partial
from threading import Lock
from typing import Any, Callable
from weakref import WeakKeyDictionary


@dataclass
class _State:
    closing: bool = False
    jobs: set[asyncio.Future] = field(default_factory=set)


@dataclass
class _Outcome:
    value: Any = None
    error: BaseException | None = None


_states: WeakKeyDictionary = WeakKeyDictionary()


def _state() -> _State:
    loop = asyncio.get_running_loop()
    return _states.setdefault(loop, _State())


def open_dispatch() -> None:
    """Open a new runtime generation only after every old worker settled."""
    state = _state()
    if state.jobs:
        raise RuntimeError("Execution workers still belong to an earlier runtime")
    state.closing = False


def pending_dispatch_count() -> int:
    """Includes completed workers until their caller releases ownership."""
    return len(_state().jobs)


async def _settle(future: asyncio.Future, *, on_cancel=None) -> Any:
    cancelled = None
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError as exc:
            # Repeated parent cancellation must not cancel the executor future.
            if on_cancel is not None:
                on_cancel()
            cancelled = cancelled or exc
        except BaseException as exc:
            if future.done():
                break  # Retrieve the worker's actual exception below.
            # Even an interrupted await cannot turn a running thread into an
            # absent order. Keep ownership, then propagate the interruption.
            if on_cancel is not None:
                on_cancel()
            cancelled = cancelled or exc
    try:
        result = future.result()
        if isinstance(result, _Outcome):
            if result.error is not None:
                raise result.error
            result = result.value
    except BaseException:
        if cancelled is not None:
            raise cancelled
        raise
    if cancelled is not None:
        raise cancelled
    return result


async def run_owned_sync(function: Callable, /, *args, **kwargs) -> Any:
    """Invoke exactly once off-loop; the caller cannot abandon a live worker."""
    state = _state()
    owner = asyncio.current_task()
    if owner is not None and owner.cancelling():
        raise asyncio.CancelledError
    if state.closing:
        raise RuntimeError("Execution dispatch is closed for shutdown")
    # A plain executor Future is not an asyncio Task: asyncio.run's bulk task
    # cancellation cannot make it look finished while its thread still runs.
    context = contextvars.copy_context()
    function = partial(function, *args, **kwargs)
    gate = Lock()
    started = cancelled_before_start = False
    def invoke():
        nonlocal started
        with gate:
            if cancelled_before_start:
                return _Outcome()  # Settle the private Future, never execute.
            started = True
        try:
            return _Outcome(value=context.run(function))
        except BaseException as exc:
            # asyncio's executor bridge may clone TimeoutError and lose custom
            # reconciliation attributes. Transport the original error as data.
            return _Outcome(error=exc)
    def cancel_queued():
        nonlocal cancelled_before_start
        with gate:
            if not started:
                cancelled_before_start = True
    future = asyncio.get_running_loop().run_in_executor(None, invoke)
    state.jobs.add(future)
    try:
        return await _settle(future, on_cancel=cancel_queued)
    finally:
        state.jobs.discard(future)


async def drain_dispatch() -> None:
    """Close admission and join every worker before publishing stopped.

    Worker failures remain the original caller's responsibility. Shutdown waits
    for all workers even after repeated cancellation, then propagates it.
    """
    state = _state()
    state.closing = True
    cancelled = None
    for future in tuple(state.jobs):
        try:
            await _settle(future)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
        except BaseException:
            pass  # The owned caller also receives/retrieves this failure.
    if cancelled is not None:
        raise cancelled
