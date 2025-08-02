# utils/rate_limiter.py
"""
Simple token-bucket rate-limiter (async + sync).

Uso habitual ─────────────────────────────────────────
>>> from utils.rate_limiter import GECKO_LIMITER
>>> async with GECKO_LIMITER:              # versión async
...     data = await get_gt_data_async(...)

>>> GECKO_LIMITER.acquire_sync()           # versión síncrona
>>> data = get_gt_data(...)

El bucket se comparte entre ambas rutas, de modo que una llamada síncrona
consume tokens exactamente igual que una asíncrona.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

__all__ = [
    "RateLimiter",
    "GECKO_LIMITER",
]

# ╭────────────────── Core implementation ───────────────────╮
class RateLimiter:
    """Token-bucket para limitar *max_calls* por *interval* segundos.

    *max_calls* ─ tokens por ventana.  
    *interval*  ─ tamaño de la ventana, en segundos (def. 60).
    """

    def __init__(self, max_calls: int, interval: float = 60.0) -> None:
        self.max_calls = max_calls
        self.interval = interval

        # estado
        self._tokens = max_calls
        self._last_reset = time.monotonic()

        # candados
        self._async_lock = asyncio.Lock()
        self._sync_lock = threading.Lock()

    # ─────────────────── async interface ────────────────────
    async def __aenter__(self) -> "RateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> Optional[bool]:
        return False  # no swallow

    async def acquire(self) -> None:
        """Consume 1 token (async). Duerme si el bucket está vacío."""
        async with self._async_lock:
            self._refill_if_needed()
            if self._tokens > 0:
                self._tokens -= 1
                return

            sleep_for = self._time_until_reset()
        await asyncio.sleep(sleep_for)          # fuera del lock

        # segunda pasada tras dormir
        async with self._async_lock:
            self._refill_if_needed()
            self._tokens -= 1

    # ──────────────── sync interface ────────────────────────
    def acquire_sync(self) -> None:
        """Consume 1 token (bloqueante)."""
        with self._sync_lock:
            self._refill_if_needed()
            if self._tokens > 0:
                self._tokens -= 1
                return

            sleep_for = self._time_until_reset()
        time.sleep(sleep_for)                    # fuera del lock

        # segunda pasada
        with self._sync_lock:
            self._refill_if_needed()
            self._tokens -= 1

    # ───────────────── helpers ──────────────────────────────
    def _refill_if_needed(self) -> None:
        now = time.monotonic()
        if now - self._last_reset >= self.interval:
            self._tokens = self.max_calls
            self._last_reset = now

    def _time_until_reset(self) -> float:
        return max(0.0, self.interval - (time.monotonic() - self._last_reset))


# ╭────────────────── Public limiter ───────────────────╮
MAX_GT_CALLS_PER_MIN = 30
GECKO_LIMITER = RateLimiter(max_calls=MAX_GT_CALLS_PER_MIN)
