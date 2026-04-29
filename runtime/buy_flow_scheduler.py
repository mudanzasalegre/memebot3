from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable


AsyncLoopFn = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class LoopSpec:
    name: str
    interval_s: float
    fn: AsyncLoopFn


async def run_resilient_loop(spec: LoopSpec) -> None:
    while True:
        try:
            await spec.fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            # The owner process already logs source-specific errors inside each loop.
            pass
        await asyncio.sleep(max(0.05, float(spec.interval_s)))


def create_loop_tasks(*specs: LoopSpec) -> list[asyncio.Task[None]]:
    return [asyncio.create_task(run_resilient_loop(spec), name=spec.name) for spec in specs]


__all__ = ["LoopSpec", "create_loop_tasks", "run_resilient_loop"]
