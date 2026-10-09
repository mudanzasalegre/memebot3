"""Owned, non-overlapping runtime work; no trading or provider implementation."""
from __future__ import annotations

import asyncio
import math
import time
from itertools import islice
from collections.abc import Callable, Coroutine, Iterable
from typing import Any


def positive_interval(value: Any, *, fallback: float = 3.0) -> float:
    try:
        result = float(value)
        if not isinstance(value, bool) and math.isfinite(result) and result > 0:
            return max(.1, result)
    except (TypeError, ValueError, OverflowError):
        pass
    return fallback


async def monitor_positions(*, ready: asyncio.Event, session_factory: Callable,
                            check: Callable, interval: Callable,
                            on_success: Callable, on_error: Callable,
                            sleep: Callable = asyncio.sleep,
                            clock: Callable = time.monotonic) -> None:
    """Wait for reconciliation, then own a fresh session for each serial tick.

    Entry evaluation never shares this session. Slow ticks do not overlap or
    create catch-up bursts. Cadence is a best-effort start-to-start target, not
    a provider-latency or execution guarantee.
    """
    await ready.wait()
    while True:
        started = clock()
        try:
            async with session_factory() as session:
                await check(session)
            on_success()
        except Exception as exc:
            on_error(exc)
        period = positive_interval(interval())
        await sleep(max(.1, period - max(0., clock() - started)))


async def evaluate_hot_queue(queue: Any, evaluate: Callable, *, max_items: int,
                             budget_s: float, clock: Callable = time.monotonic) -> int:
    """Pop just in time; retain the untouched tail for newer updates/expiry.

    Never cancel an in-flight entry to satisfy the scheduling budget. Its own
    execution guard owns its timeout. The budget bounds how much *new* work a
    cycle admits and leaves the remaining opportunities queued, not discarded.
    """
    def next_item():
        batch = queue.pop_batch(1, expand=False)
        return batch[0] if batch else None
    return await evaluate_ready_queue(next_item, evaluate, max_items=max_items,
        budget_s=budget_s, clock=clock, identity=None)


async def admit_hot_candidates(queue: Any, tokens: Iterable, *, source: str = "pumpfun",
                               sleep: Callable = asyncio.sleep) -> int:
    """Bound synchronous discovery work and yield between128-row chunks.

    Do not bulk-pop the evaluation tail or launch parallel queue mutations.
    Position supervision can run between chunks. No provider latency SLA is
    implied: checked artifacts and payloads still need production profiling.
    """
    incoming, admitted = iter(tokens), 0
    chunk = list(islice(incoming, 128))
    while chunk:
        admitted += sum(queue.add_many(chunk, source=source))
        chunk = list(islice(incoming, 128))
        if chunk:
            await sleep(0)
    return admitted


async def evaluate_ready_queue(next_item: Callable, evaluate: Callable, *, max_items: int,
                               budget_s: float, clock: Callable = time.monotonic,
                               identity: Callable | None = lambda item: item) -> int:
    """Incremental service; rotating queues visit each identity once per cycle.

    A popping queue can disable deduplication: a new update received while an
    evaluation awaits is still eligible. Item limits remain caller-configured.
    """
    started, count = clock(), 0
    seen = set()
    budget = positive_interval(budget_s)
    for _ in range(max(0, int(max_items))):
        if count and clock() - started >= budget:
            break
        item = next_item()
        if item is None:
            break
        if identity is not None:
            key = identity(item)
            if key in seen:
                break
            seen.add(key)
        await evaluate(item)
        count += 1
    return count


async def supervise(coroutines: Iterable[tuple[str, Coroutine]]) -> None:
    """A critical loop ending cancels and drains every owned sibling."""
    tasks = [asyncio.create_task(coro, name=name) for name, coro in coroutines]
    try:
        if not tasks:
            return
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            if task in done:
                task.result()  # Propagate original failure/cancellation, not an ExceptionGroup.
        raise RuntimeError("Critical runtime loop ended: " + ", ".join(t.get_name() for t in tasks if t in done))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
