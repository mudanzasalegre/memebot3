# utils/rate_limiter.py
"""
Limitadores de ritmo para el bot.

Incluye dos implementaciones complementarias:

1) RateLimiter (token-bucket, async + sync)
   - Útil para limitar llamadas a APIs a un ritmo fijo (p.ej. 30/min).
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
import threading
import time
from collections import deque
from typing import Deque, Optional

__all__ = [
    "RateLimiter",
    "LeakyBucket",
    "GECKO_LIMITER",
]

# ╭────────────────── RateLimiter (token-bucket) ───────────────────╮
class RateLimiter:
    """Token-bucket para limitar *max_calls* por *interval* segundos.

    *max_calls* ─ tokens por ventana.
    *interval*  ─ tamaño de la ventana, en segundos (def. 60).
    """

    def __init__(self, max_calls: int, interval: float = 60.0) -> None:
        if max_calls <= 0:
            raise ValueError("max_calls debe ser > 0")
        if interval <= 0:
            raise ValueError("interval debe ser > 0")

        self.max_calls = int(max_calls)
        self.interval = float(interval)

        # estado
        self._tokens = self.max_calls
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

        # dormir fuera del lock
        await asyncio.sleep(sleep_for)

        # segunda pasada tras dormir
        async with self._async_lock:
            self._refill_if_needed()
            # garantizado >=1 token
            self._tokens = max(0, self._tokens - 1) if self._tokens > 0 else self.max_calls - 1
            if self._tokens < 0:
                self._tokens = 0  # no negativo por seguridad

    # ──────────────── sync interface ────────────────────────
    def acquire_sync(self) -> None:
        """Consume 1 token (bloqueante)."""
        with self._sync_lock:
            self._refill_if_needed()
            if self._tokens > 0:
                self._tokens -= 1
                return
            sleep_for = self._time_until_reset()

        # dormir fuera del lock
        time.sleep(sleep_for)

        # segunda pasada
        with self._sync_lock:
            self._refill_if_needed()
            self._tokens = max(0, self._tokens - 1) if self._tokens > 0 else self.max_calls - 1
            if self._tokens < 0:
                self._tokens = 0

    # ───────────────── helpers ──────────────────────────────
    def _refill_if_needed(self) -> None:
        now = time.monotonic()
        if now - self._last_reset >= self.interval:
            self._tokens = self.max_calls
            self._last_reset = now

    def _time_until_reset(self) -> float:
        return max(0.0, self.interval - (time.monotonic() - self._last_reset))


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
# Límite típico para llamadas a GeckoTerminal (30 req/min).
MAX_GT_CALLS_PER_MIN = 30
GECKO_LIMITER = RateLimiter(max_calls=MAX_GT_CALLS_PER_MIN)
