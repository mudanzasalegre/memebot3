"""
analytics.trend
~~~~~~~~~~~~~~~
Calcula una señal de tendencia muy ligera (up / down / flat / unknown)
a partir de las velas de 5 min de DexScreener.

• Early-exit si el endpoint responde 404 (token sin histórico todavía).
• Respuesta cacheada (in-memory) para no golpear en exceso.
"""

from __future__ import annotations

import asyncio
import logging
import random
from statistics import mean
from typing import Literal, List

import aiohttp

from config import DEX_API_BASE
from utils.simple_cache import cache_get, cache_set

log = logging.getLogger("trend")

# ——————————————————— Parámetros ———————————————————
EMA_FAST       = 7          # 7 velas   ≈ 35 min
EMA_SLOW       = 21         # 21 velas  ≈ 105 min
_MAX_TRIES     = 3
_BACKOFF_BASE  = 1.0        # segundos
_TIMEOUT       = 10         # timeout total petición HTTP
_CACHE_TTL_OK  = 90         # 1½ min si todo OK
_CACHE_TTL_ERR = 300        # 5 min en caso de error/404


# —————————————————— helpers internos ——————————————————
def _ema(series: List[float], length: int) -> float:
    """EMA clásica (sin depender de pandas)."""
    if len(series) < length:
        return mean(series) if series else 0.0

    k   = 2 / (length + 1)
    ema = series[0]
    for price in series[1:]:
        ema = price * k + ema * (1 - k)
    return ema


async def _fetch_closes(address: str) -> List[float]:
    """
    Descarga hasta 200 cierres de velas 5 m.  
    Retrys exponenciales salvo que el primer intento sea 404.
    """
    url      = f"{DEX_API_BASE.rstrip('/')}/chart/solana/{address}?interval=5m&limit=200"
    backoff  = _BACKOFF_BASE

    for attempt in range(1, _MAX_TRIES + 1):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT)
            ) as sess, sess.get(url) as resp:

                if resp.status == 404:
                    raise FileNotFoundError("DexScreener 404 — sin velas aún")

                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")

                data   = await resp.json()
                closes = [float(c["close"]) for c in data if c.get("close")]
                if closes:
                    return closes
                raise RuntimeError("respuesta vacía")

        except FileNotFoundError as e_404:
            log.debug("[trend] %s 404 — no reintento", address[:4])
            raise e_404  # se captura arriba y se cachea como unknown
        except Exception as exc:  # noqa: BLE001
            log.debug("[trend] %s intento %s/%s → %s",
                      address[:4], attempt, _MAX_TRIES, exc)
            if attempt == _MAX_TRIES:
                raise
            await asyncio.sleep(backoff + random.random() * 0.5)
            backoff *= 2

    return []  # pragma: no cover


# —————————————————— API pública ——————————————————
async def trend_signal(address: str) -> Literal["up", "down", "flat", "unknown"]:
    """
    Devuelve la tendencia actual del token:
        • "up"   : EMA rápida > EMA lenta * 1.02
        • "down" : EMA rápida < EMA lenta * 0.98
        • "flat" : dentro del rango        ±2 %
        • "unknown" : sin datos o error
    El resultado se cachea (_CACHE_TTL_OK / _CACHE_TTL_ERR).
    """
    cache_key = f"trend:{address}"
    if (hit := cache_get(cache_key)) is not None:
        return hit

    try:
        closes = await _fetch_closes(address)
    except Exception:
        cache_set(cache_key, "unknown", ttl=_CACHE_TTL_ERR)
        return "unknown"

    if len(closes) < EMA_SLOW:        # histórico insuficiente
        cache_set(cache_key, "unknown", ttl=_CACHE_TTL_ERR)
        return "unknown"

    fast = _ema(closes[-EMA_FAST * 3:], EMA_FAST)
    slow = _ema(closes[-EMA_SLOW * 3:], EMA_SLOW)

    if fast > slow * 1.02:
        signal = "up"
    elif fast < slow * 0.98:
        signal = "down"
    else:
        signal = "flat"

    cache_set(cache_key, signal, ttl=_CACHE_TTL_OK)
    return signal


# ——————————— CLI de prueba rápida ———————————
if __name__ == "__main__":  # pragma: no cover
    import sys
    addr = sys.argv[1] if len(sys.argv) > 1 else "So11111111111111111111111111111111111111112"
    print(asyncio.run(trend_signal(addr)))
