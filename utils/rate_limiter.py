# utils/rate_limiter.py
"""
Limitadores de ritmo para el bot.

Incluye dos implementaciones complementarias:

1) RateLimiter (ventana fija compartida, async + sync)
   - Útil para limitar llamadas a APIs a un ritmo fijo (p.ej.10/min).
   - Soporta uso como context manager async y vía método síncrono.

   Uso:
   >>> from utils.rate_limiter import GECKO_LIMITER
   >>> async with GECKO_LIMITER:              # versión async
   ...     data = await get_gt_data_async(...)
   >>> GECKO_LIMITER.acquire_sync()           # versión síncrona
   >>> data = get_gt_data(...)

2) LeakyBucket (ventana deslizante, sólo sync)
   - Útil para limitar eventos “ráfaga” (p.ej. compras) a N por ventana.
   - O(1) amortizado para limpiar eventos fuera de ventana.

   Uso:
   >>> from utils.rate_limiter import LeakyBucket
   >>> BUY_LIMITER = LeakyBucket(max_hits=3, window_s=120)
   >>> if BUY_LIMITER.allow():     # consume 1 “hit” si cabe
   ...     do_buy()
   >>> BUY_LIMITER.current()       # cuántos hits hay dentro de la ventana
"""

from __future__ import annotations

import asyncio
import math
from utils.bounded_state import bounded_int
import os
import threading
import time
from collections import deque
from typing import Deque, Optional

__all__ = [
    "RateLimiter",
    "LeakyBucket",
    "GECKO_LIMITER",
]

# ╭────────────────── RateLimiter (ventana fija) ───────────────────╮
class RateLimiter:
    """Shared monotonic fixed-window admission; every wakeup retries permission."""

    def __init__(self, max_calls: int, interval: float = 60.0) -> None:
        if (isinstance(max_calls, bool) or isinstance(interval, bool)
                or not math.isfinite(float(max_calls)) or not math.isfinite(float(interval))
                or float(max_calls) < 1 or not float(max_calls).is_integer() or float(interval) <= 0):
            raise ValueError("Invalid rate window")
        self.max_calls, self.interval = int(max_calls), float(interval)
        self._tokens = self.max_calls
        self._lock = threading.RLock()
        with self._lock:
            self._last_reset = self._last_observed = self._now_locked()

    @staticmethod
    def _now_locked():
        """Read only under admission lock; invalid clocks cannot mutate state."""
        now = time.monotonic()
        try:
            valid = type(now) in (int, float) and math.isfinite(now)
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            raise ValueError("Invalid local monotonic clock")
        return now

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def _permission_or_wait(self):
        with self._lock:
            # Clock read and state update must share the lock across sync/
            # async callers: delayed pre-lock reads are not real reversals.
            now = self._now_locked()
            if now < self._last_observed:
                self._tokens, self._last_reset = 0, now
            self._last_observed = now
            if now - self._last_reset >= self.interval:
                self._tokens, self._last_reset = self.max_calls, now
            if self._tokens > 0:
                self._tokens -= 1
                return 0.
            return max(0., self.interval - (now - self._last_reset))

    async def acquire(self):
        while (wait := self._permission_or_wait()) > 0:
            await asyncio.sleep(wait)

    def acquire_sync(self):
        while (wait := self._permission_or_wait()) > 0:
            time.sleep(wait)


# ╭────────────────── LeakyBucket (ventana deslizante) ──────────────╮
class LeakyBucket:
    """Rate-limiter por ventana deslizante (sliding window).

    Permite hasta *max_hits* eventos dentro de los últimos *window_s* segundos.
    `allow(n)` intenta reservar *n* eventos a la vez (por defecto 1).

    Implementación:
      - Mantiene una deque de timestamps (monotonic) de eventos admitidos.
      - En cada `allow()` purga los timestamps antiguos y comprueba capacidad.

    Thread-safe (lock interno); invocación síncrona (válida para hilos y corutinas).
    """

    def __init__(self, max_hits: int, window_s: int | float) -> None:
        if max_hits <= 0:
            raise ValueError("max_hits debe ser > 0")
        if window_s <= 0:
            raise ValueError("window_s debe ser > 0")

        self.max_hits: int = int(max_hits)
        self.window_s: float = float(window_s)
        self._events: Deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self, n: int = 1) -> bool:
        """Intenta consumir *n* unidades. Devuelve True si se acepta, False si no."""
        if n <= 0:
            return True  # trivial: no consume nada
        now = time.monotonic()
        with self._lock:
            self._drain_locked(now)
            # capacidad disponible
            if len(self._events) + n <= self.max_hits:
                # reservar n eventos con timestamp 'now'
                # (suficiente precisión; evita varias lecturas de monotonic)
                for _ in range(n):
                    self._events.append(now)
                return True
            return False

    def current(self) -> int:
        """Número de eventos contabilizados actualmente en la ventana."""
        now = time.monotonic()
        with self._lock:
            self._drain_locked(now)
            return len(self._events)

    # ───────────────── helpers ──────────────────────────────
    def _drain_locked(self, now: float) -> None:
        """Elimina eventos fuera de ventana; requiere lock tomado."""
        cutoff = now - self.window_s
        ev = self._events
        while ev and ev[0] <= cutoff:
            ev.popleft()


# ╭────────────────── Limiter público de ejemplo ───────────────────╮
# Límite típico para llamadas a GeckoTerminal (approximately10 req/min).
try:
    MAX_GT_CALLS_PER_MIN = bounded_int(os.getenv("GECKO_RATE_LIMIT", "10"), 10, 1, 10)
except Exception:
    MAX_GT_CALLS_PER_MIN = 10
GECKO_LIMITER = RateLimiter(max_calls=MAX_GT_CALLS_PER_MIN)
