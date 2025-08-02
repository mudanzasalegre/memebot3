# memebot3/fetcher/birdeye.py
"""
Fetcher para Birdeye (https://birdeye.so)

• FREE tier ≈ 1 req/s (60 RPM) – controlado por un throttle cooperativo.
• Expone:
      get_token_info(address)   → métrica de token (priceUsd, liquidityUsd…)
      get_pool_info(address)    → métricas a nivel pool (tvlUsd, volume24hUsd…)

  Ambos devuelven siempre un ``dict`` con las *claves originales de Birdeye*
  o ``None`` si la petición falla / no hay datos.  El mapping a nombres
  canónicos lo realiza utils.fallback.fill_missing_fields() en price_service.

➤ .env requerido:
────────────────────────────────────────────────────────
BIRDEYE_API_KEY=<tu-key>
BIRDEYE_RPM=60            # opcional, default 60
USE_BIRDEYE=true          # ya lo usa utils.price_service
────────────────────────────────────────────────────────
"""

from __future__ import annotations

import aiohttp
import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

# ───────────────────────── Config / constantes ──────────────────────────
_API_KEY:   Optional[str] = os.getenv("BIRDEYE_API_KEY")
_BASE_URL:  str           = "https://public-api.birdeye.so/public"

# Límite de peticiones por minuto (default 60 → 1 RPS)
_RPM:          int   = max(int(os.getenv("BIRDEYE_RPM", "60")), 1)
_MIN_INTERVAL: float = 60.0 / _RPM          # seg. entre llamadas

# Endpoints Birdeye (token / pool)
_TOKEN_EP: str = "/token/{addr}"
_POOL_EP:  str = "/pool/{addr}"             # ← según docs públicas

log = logging.getLogger("birdeye")

# rate-limit cooperativo (global en proceso)
_last_call_ts: float       = 0.0
_lock:          asyncio.Lock = asyncio.Lock()

# ───────────────────────── Helpers ───────────────────────────────────────
async def _throttle() -> None:
    """Enforce RPM; bloquea si la última llamada es muy reciente."""
    global _last_call_ts

    async with _lock:                     # evita carreras entre corrutinas
        elapsed   = time.monotonic() - _last_call_ts
        wait_for  = _MIN_INTERVAL - elapsed
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        _last_call_ts = time.monotonic()


async def _fetch(endpoint: str) -> Dict[str, Any] | None:
    """
    GET <BASE_URL><endpoint> con cabecera Authorization.

    Devuelve el ``dict`` contenido en ``"data"`` o None.
    (Errores temporales → los maneja el caller con TTL-cache).
    """
    if not _API_KEY:
        log.debug("[birdeye] desactivado – no hay API key")
        return None

    await _throttle()

    url     = f"{_BASE_URL}{endpoint}"
    headers = {"Authorization": f"Bearer {_API_KEY}"}

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers=headers) as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    return payload.get("data") or {}
                log.debug("[birdeye] %s → HTTP %s", endpoint, resp.status)
    except Exception as exc:                    # noqa: BLE001
        log.debug("[birdeye] request error %s → %s", endpoint, exc)

    return None

# ───────────────────────── API pública ────────────────────────────────────
async def get_token_info(address: str) -> Dict[str, Any] | None:
    """
    ``/token/{address}``   – precio, liquidez, mcap, volumen 24h…

    Devuelve dict o ``None``.
    """
    data = await _fetch(_TOKEN_EP.format(addr=address))
    if data:
        log.debug("Birdeye token %s | priceUsd %.6g  liqUsd %.0f",
                  address[:4], data.get("priceUsd"), data.get("liquidityUsd"))
    return data


async def get_pool_info(address: str) -> Dict[str, Any] | None:
    """
    ``/pool/{address}``    – stats de pool (TVL, volumen, fees, APR…)

    Útil cuando un token tiene varias pools y se quiere info específica
    del AMM.  Devuelve dict o ``None``.
    """
    data = await _fetch(_POOL_EP.format(addr=address))
    if data:
        log.debug("Birdeye pool  %s | tvlUsd %.0f  vol24hUsd %.0f",
                  address[:4], data.get("tvlUsd"), data.get("volume24hUsd"))
    return data


__all__ = [
    "get_token_info",
    "get_pool_info",
]
