"""
utils.simple_cache
~~~~~~~~~~~~~~~~~~
Caché en memoria con TTL muy ligero y _thread-safe_ para corutinas.
No persiste entre ejecuciones.

Uso:
    from utils.simple_cache import cache_get, cache_set

    v = cache_get("clave")
    if v is None:
        v = await algo_costoso()
        cache_set("clave", v, ttl=60)
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Tuple

# clave → (expira_at, valor)
_CACHE: Dict[str, Tuple[float, Any]] = {}
_LOCK = asyncio.Lock()  # evita carreras simples


def cache_get(key: str) -> Any | None:
    exp, val = _CACHE.get(key, (0.0, None))
    if exp > time.time():
        return val
    # expirado → lo quitamos
    _CACHE.pop(key, None)
    return None


def cache_set(key: str, value: Any, ttl: int = 60) -> None:
    _CACHE[key] = (time.time() + ttl, value)


async def cache_get_or_set(key: str, coro, ttl: int = 60):
    """
    Variante async: si no existe, evalúa la coroutine `coro()` y guarda.
    """
    async with _LOCK:
        hit = cache_get(key)
        if hit is not None:
            return hit
        value = await coro()
        cache_set(key, value, ttl)
        return value
