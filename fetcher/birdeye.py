# memebot3/fetcher/birdeye.py
"""
Fetcher para Birdeye (https://birdeye.so)

• FREE tier ≈ 1 req/s (60 RPM) – controlado por un throttle cooperativo.
• Expone:
      get_token_info(address)   → métricas de token (priceUsd, liquidityUsd…)
      get_pool_info(address)    → métricas a nivel pool (tvlUsd, volume24hUsd…)

  Ambos devuelven siempre un ``dict`` con las *claves originales de Birdeye*
  o ``None`` si la petición falla / no hay datos.  
  El mapping a nombres canónicos lo realiza utils.fallback.fill_missing_fields() en price_service.

Mejoras 2025-08-09
──────────────────
• TTL adaptable para “sin datos”:
      BIRDEYE_TTL_NIL_SHORT (def. 90 s) → 3 primeros fallos consecutivos.
      BIRDEYE_TTL_NIL_MAX   (def. 300 s) → del 4º fallo en adelante.
• Contador de fallos por clave endpoint+address.
• Cacheo en memoria con utils.simple_cache.
"""

from __future__ import annotations

import aiohttp
import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

from utils.simple_cache import cache_get, cache_set

# ───────────────────────── Config / constantes ──────────────────────────
_API_KEY:   Optional[str] = os.getenv("BIRDEYE_API_KEY")
_BASE_URL:  str           = "https://public-api.birdeye.so/public"

# Límite de peticiones por minuto (default 60 → 1 RPS)
_RPM:          int   = max(int(os.getenv("BIRDEYE_RPM", "60")), 1)
_MIN_INTERVAL: float = 60.0 / _RPM          # seg. entre llamadas

# TTL adaptativo para “sin datos”
_TTL_NIL_SHORT = int(os.getenv("BIRDEYE_TTL_NIL_SHORT", "90"))
_TTL_NIL_MAX   = int(os.getenv("BIRDEYE_TTL_NIL_MAX", "300"))
_SENTINEL_NIL  = object()

# Endpoints Birdeye (token / pool)
_TOKEN_EP: str = "/token/{addr}"
_POOL_EP:  str = "/pool/{addr}"

log = logging.getLogger("birdeye")

# rate-limit cooperativo (global en proceso)
_last_call_ts: float         = 0.0
_lock:          asyncio.Lock = asyncio.Lock()

# contador de fallos consecutivos
_fail_count: dict[str, int] = {}


# ───────────────────────── Helpers ───────────────────────────────────────
async def _throttle() -> None:
    """Enforce RPM; bloquea si la última llamada es muy reciente."""
    global _last_call_ts
    async with _lock:
        elapsed   = time.monotonic() - _last_call_ts
        wait_for  = _MIN_INTERVAL - elapsed
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        _last_call_ts = time.monotonic()


def _register_fail(key: str) -> None:
    fails = _fail_count.get(key, 0) + 1
    _fail_count[key] = fails
    ttl = _TTL_NIL_MAX if fails >= 4 else _TTL_NIL_SHORT
    cache_set(key, _SENTINEL_NIL, ttl=ttl)
    log.debug("[birdeye] %s → sin datos (TTL=%ss, fallos=%d)", key, ttl, fails)


def _reset_fail(key: str) -> None:
    _fail_count.pop(key, None)


async def _fetch(endpoint: str, cache_key: str) -> Dict[str, Any] | None:
    """
    GET <BASE_URL><endpoint> con cabecera Authorization.

    Devuelve el ``dict`` contenido en ``"data"`` o None.
    Usa TTL adaptable en caso de NIL para controlar reintentos.
    """
    if not _API_KEY:
        log.debug("[birdeye] desactivado – no hay API key")
        return None

    # cache hit
    hit = cache_get(cache_key)
    if hit is not None:
        return None if hit is _SENTINEL_NIL else hit

    await _throttle()

    url     = f"{_BASE_URL}{endpoint}"
    headers = {"Authorization": f"Bearer {_API_KEY}"}

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers=headers) as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    data = payload.get("data") or {}
                    _reset_fail(cache_key)
                    cache_set(cache_key, data, ttl=60)  # TTL corto para datos OK
                    return data
                log.debug("[birdeye] %s → HTTP %s", endpoint, resp.status)
    except Exception as exc:
        log.debug("[birdeye] request error %s → %s", endpoint, exc)

    _register_fail(cache_key)
    return None


# ───────────────────────── API pública ────────────────────────────────────
async def get_token_info(address: str) -> Dict[str, Any] | None:
    """
    ``/token/{address}``   – precio, liquidez, mcap, volumen 24h…
    """
    key = f"be:token:{address}"
    data = await _fetch(_TOKEN_EP.format(addr=address), key)
    if data:
        log.debug("Birdeye token %s | priceUsd %.6g  liqUsd %.0f",
                  address[:4], data.get("priceUsd"), data.get("liquidityUsd"))
    return data


async def get_pool_info(address: str) -> Dict[str, Any] | None:
    """
    ``/pool/{address}``    – stats de pool (TVL, volumen, fees, APR…)
    """
    key = f"be:pool:{address}"
    data = await _fetch(_POOL_EP.format(addr=address), key)
    if data:
        log.debug("Birdeye pool  %s | tvlUsd %.0f  vol24hUsd %.0f",
                  address[:4], data.get("tvlUsd"), data.get("volume24hUsd"))
    return data


__all__ = [
    "get_token_info",
    "get_pool_info",
]
