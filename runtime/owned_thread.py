"""A caller owns native thread completion, including repeated cancellation."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


async def await_owned_thread(function: Callable, *args: Any, **kwargs: Any) -> Any:
    """Drain admitted work before cancellation escapes; never kill a thread."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        # Preserve the original caller cancellation, but consume worker errors.
        if worker.done():
            try:
                worker.result()
            except BaseException:
                pass
        raise
