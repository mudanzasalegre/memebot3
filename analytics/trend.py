"""
analytics.trend
~~~~~~~~~~~~~~~
Calcula una señal de tendencia (up / down / flat / unknown) a partir de las
velas de 5 min de DexScreener.

• Retry exponencial (máx. 3) salvo que el primer intento sea 404.
• Cache in-memory (_CACHE_TTL_OK / _CACHE_TTL_ERR) para no golpear en exceso.
• Si no hay histórico suficiente ⇒ "unknown".
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import List, Literal

import aiohttp

from config import DEX_API_BASE
from utils.simple_cache import cache_get, cache_set

log = logging.getLogger("trend")

# ———————— parámetros —————————
EMA_FAST = 7          # 7 velas ≈ 35 min
EMA_SLOW = 21         # 21 velas ≈ 105 min
_MAX_TRIES = 3
_BACKOFF = 1.0        # s inicial back-off
_TIMEOUT = 10         # s HTTP
_CACHE_TTL_OK = 90    # 1 ½ min si todo OK
_CACHE_TTL_ERR = 300  # 5 min en error/404

# ———————— helpers —————————
def _ema(series: List[float], length: int) -> float:
    """EMA sencilla, sin pandas."""
    if not series:
        return 0.0
    k = 2 / (length + 1)
    ema = series[0]
    for p in series[1:]:
        ema = p * k + ema * (1 - k)
    return ema


async def _fetch_closes(address: str) -> List[float]:
    """
    Devuelve hasta 200 cierres de velas 5 m.  
    Lanza FileNotFoundError si DexScreener devuelve 404.
    """
    url = f"{DEX_API_BASE.rstrip('/')}/chart/solana/{address}?interval=5m&limit=200"
    backoff = _BACKOFF

    for attempt in range(1, _MAX_TRIES + 1):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT)
            ) as sess, sess.get(url) as resp:

                if resp.status == 404:
                    raise FileNotFoundError("DexScreener 404 (sin velas)")

                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")

                data = await resp.json()
                closes = [float(c["close"]) for c in data if c.get("close")]
                if closes:
                    return closes
                raise RuntimeError("respuesta vacía")

        except FileNotFoundError:
            log.debug("[trend] %s 404 – no reintento", address[:4])
            raise
        except Exception as exc:
            log.debug("[trend] %s intento %s/%s → %s",
                      address[:4], attempt, _MAX_TRIES, exc)
            if attempt == _MAX_TRIES:
                raise
            await asyncio.sleep(backoff + random.random() * 0.5)
            backoff *= 2

    return []        # never reached (pragma: no cover)


# ———————— API pública —————————
async def trend_signal(address: str) -> Literal["up", "down", "flat", "unknown"]:
    """
    • "up"   : EMA faster > EMA slower × 1.02
    • "down" : EMA faster < EMA slower × 0.98
    • "flat" : dentro del rango ±2 %
    • "unknown" : sin datos / error
    """
    ck = f"trend:{address}"
    if (hit := cache_get(ck)) is not None:
        return hit

    try:
        closes = await _fetch_closes(address)
    except Exception:
        cache_set(ck, "unknown", ttl=_CACHE_TTL_ERR)
        return "unknown"

    if len(closes) < EMA_SLOW:
        cache_set(ck, "unknown", ttl=_CACHE_TTL_ERR)
        return "unknown"

    fast = _ema(closes[-EMA_FAST * 3:], EMA_FAST)
    slow = _ema(closes[-EMA_SLOW * 3:], EMA_SLOW)

    if fast > slow * 1.02:
        sig = "up"
    elif fast < slow * 0.98:
        sig = "down"
    else:
        sig = "flat"

    cache_set(ck, sig, ttl=_CACHE_TTL_OK)
    return sig


# ——— CLI de prueba rápida —————————
if __name__ == "__main__":  # pragma: no cover
    import sys
    addr = sys.argv[1] if len(sys.argv) > 1 else "So11111111111111111111111111111111111111112"
    print(asyncio.run(trend_signal(addr)))
