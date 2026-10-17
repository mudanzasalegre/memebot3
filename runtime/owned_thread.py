"""A caller owns native thread completion, including repeated cancellation."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


async def await_owned_task(coroutine, *, name: str | None = None):
    """Admit once and retain asynchronous work through repeated cancellation.

    The coroutine itself must drain the concrete resource it creates. A done
    task alone is not proof that a subprocess, thread or order has settled.
    """
    import inspect
    if not inspect.iscoroutine(coroutine):
        raise TypeError('Expected an owned coroutine')
    owner = asyncio.current_task()
    if owner is not None and owner.cancelling():
        coroutine.close()
        raise asyncio.CancelledError
    try:
        worker = asyncio.create_task(coroutine, name=name)
    except BaseException:
        coroutine.close()
        raise
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancelled:
        if worker.cancelled() and (owner is None or not owner.cancelling()):
            raise RuntimeError('Owned asynchronous work was cancelled internally') from cancelled
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            worker.result()
        except BaseException:
            pass
        raise


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
