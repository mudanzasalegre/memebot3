"""
analytics.trend
~~~~~~~~~~~~~~~
Señal de tendencia para MemeBot 3.

1️⃣  Intenta calcularla con dos EMAs (7 × 5 m y 21 × 5 m) sobre las velas de
    DexScreener ⇒ «up / down / flat».
2️⃣  Si no hay velas suficientes (token muy nuevo) **o** ocurre un error de
    red, hace *fallback* a la heurística rápida:

        • +15 % en 5 m ⇒ "up"
        • –15 % en 5 m ⇒ "down"
        • en medio      ⇒ "flat"

Cache in‑memory para no machacar la API.

*Mod 26‑Jul‑2025*: se añade la excepción `Trend404Retry` para delegar el manejo
                   de 404 (sin velas) al `requeue_policy`.
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

# —————————————————— Parámetros ——————————————————
EMA_FAST = 7           # 7 velas = 35 min
EMA_SLOW = 21          # 21 velas = 105 min
_MAX_TRIES = 3
_BACKOFF = 1.0         # s inicial back‑off
_TIMEOUT = 10          # s HTTP
_CACHE_TTL_OK  = 90    # 1 ½ min si todo OK
_CACHE_TTL_ERR = 300   # 5 min en error/404

# —————————————————— Nueva excepción ————————————
class Trend404Retry(Exception):
    """El endpoint /chart no tiene velas aún → reintentar más tarde."""

# —————————————————— Helpers ————————————————————

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
    """Devuelve hasta 200 cierres de velas 5 m. Lanza Trend404Retry si 404."""
    url = f"{DEX_API_BASE.rstrip('/')}/chart/solana/{address}?interval=5m&limit=200"
    backoff = _BACKOFF

    for attempt in range(1, _MAX_TRIES + 1):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT)
            ) as sess, sess.get(url) as resp:

                if resp.status == 404:
                    raise Trend404Retry("DexScreener 404 – sin velas todavía")

                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")

                data = await resp.json()
                closes = [float(c["close"]) for c in data if c.get("close")]
                if closes:
                    return closes
                raise RuntimeError("respuesta vacía")

        except Trend404Retry:
            log.debug("[trend] %s 404 – delego requeue", address[:4])
            raise
        except Exception as exc:
            log.debug("[trend] %s intento %s/%s → %s",
                      address[:4], attempt, _MAX_TRIES, exc)
            if attempt == _MAX_TRIES:
                raise
            await asyncio.sleep(backoff + random.random() * 0.5)
            backoff *= 2

    return []  # pragma: no cover


# —————————————————— API pública ——————————————————
async def trend_signal(address: str) -> Literal["up", "down", "flat", "unknown"]:
    """Calcula la señal o propaga Trend404Retry si no hay velas."""
    ck = f"trend:{address}"
    if (hit := cache_get(ck)) is not None:
        return hit

    # 1) —— intento con velas de 5 m ————————————————————
    try:
        closes = await _fetch_closes(address)
    except Trend404Retry:
        # Propagar para que el orquestador pueda reencolar
        raise
    except Exception:
        closes = []                         # fuerza fallback

    if len(closes) >= EMA_SLOW:
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

    # 2) —— fallback ±15 % en 5 m ————————————————
    from fetcher import dexscreener  # import local p/ evitar ciclo
    pair = await dexscreener.get_pair(address)
    pct5 = 0.0
    if pair:
        pct5 = float(
            pair.get("price_pct_5m")
            or pair.get("priceChange", {}).get("m5")
            or 0
        )

    if pct5 >= 15:
        sig = "up"
    elif pct5 <= -15:
        sig = "down"
    else:
        sig = "flat"

    ttl = _CACHE_TTL_OK if pair else _CACHE_TTL_ERR
    cache_set(ck, sig, ttl=ttl)
    return sig

# ————————— CLI de prueba rápida ——————————
if __name__ == "__main__":  # pragma: no cover
    import sys
    test_addr = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "So11111111111111111111111111111111111111112"
    )
    print(asyncio.run(trend_signal(test_addr)))