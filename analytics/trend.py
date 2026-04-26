from __future__ import annotations

import asyncio
import logging
import random
from typing import List, Literal

import aiohttp

from config import DEX_API_BASE
from utils.simple_cache import cache_get, cache_set

log = logging.getLogger("trend")

EMA_FAST = 7
EMA_SLOW = 21
_MAX_TRIES = 3
_BACKOFF = 1.0
_TIMEOUT = 10
_CACHE_TTL_OK = 90
_CACHE_TTL_ERR = 300


class Trend404Retry(Exception):
    """El endpoint /chart no tiene velas aun."""


def _ema(series: List[float], length: int) -> float:
    if not series:
        return 0.0
    k = 2 / (length + 1)
    ema = series[0]
    for price in series[1:]:
        ema = price * k + ema * (1 - k)
    return ema


async def _fetch_closes(address: str) -> List[float]:
    url = f"{DEX_API_BASE.rstrip('/')}/chart/solana/{address}?interval=5m&limit=200"
    backoff = _BACKOFF

    for attempt in range(1, _MAX_TRIES + 1):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT)
            ) as sess, sess.get(url) as resp:
                if resp.status == 404:
                    if attempt == 1:
                        from fetcher import dexscreener

                        pair = await dexscreener.get_pair(address)
                        if pair:
                            return []
                        raise Trend404Retry("DexScreener 404 - sin velas todavia")
                    log.debug("Trend 404 repetido - sigo sin trend")
                    return []

                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")

                data = await resp.json()
                closes = [float(c["close"]) for c in data if c.get("close")]
                if closes:
                    return closes
                raise RuntimeError("respuesta vacia")
        except Trend404Retry:
            log.debug("[trend] %s 404 - delego requeue", address[:4])
            raise
        except Exception as exc:
            log.debug("[trend] %s intento %s/%s -> %s", address[:4], attempt, _MAX_TRIES, exc)
            if attempt == _MAX_TRIES:
                raise
            await asyncio.sleep(backoff + random.random() * 0.5)
            backoff *= 2

    return []


async def trend_signal(address: str) -> tuple[Literal["up", "down", "flat", "unknown"], bool]:
    ck = f"trend:{address}"
    if (hit := cache_get(ck)) is not None:
        if isinstance(hit, tuple) and len(hit) == 2:
            return hit
        return hit, False

    fallback_used = False
    try:
        closes = await _fetch_closes(address)
    except Trend404Retry:
        raise
    except Exception:
        closes = []

    if len(closes) >= EMA_SLOW:
        fast = _ema(closes[-EMA_FAST * 3 :], EMA_FAST)
        slow = _ema(closes[-EMA_SLOW * 3 :], EMA_SLOW)

        if fast > slow * 1.02:
            sig: Literal["up", "down", "flat", "unknown"] = "up"
        elif fast < slow * 0.98:
            sig = "down"
        else:
            sig = "flat"

        cache_set(ck, (sig, fallback_used), ttl=_CACHE_TTL_OK)
        return sig, fallback_used

    from fetcher import dexscreener

    pair = await dexscreener.get_pair(address)
    if not pair:
        sig = "unknown"
        fallback_used = True
        cache_set(ck, (sig, fallback_used), ttl=_CACHE_TTL_ERR)
        return sig, fallback_used

    pct5_raw = pair.get("price_pct_5m")
    if pct5_raw is None:
        pct5_raw = (pair.get("priceChange") or {}).get("m5")

    try:
        pct5 = float(pct5_raw)
    except Exception:
        sig = "unknown"
        fallback_used = True
        cache_set(ck, (sig, fallback_used), ttl=_CACHE_TTL_ERR)
        return sig, fallback_used

    if pct5 >= 15:
        sig = "up"
    elif pct5 <= -15:
        sig = "down"
    else:
        sig = "flat"

    fallback_used = True
    cache_set(ck, (sig, fallback_used), ttl=_CACHE_TTL_OK)
    return sig, fallback_used


if __name__ == "__main__":  # pragma: no cover
    import sys

    test_addr = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "So11111111111111111111111111111111111111112"
    )
    print(asyncio.run(trend_signal(test_addr)))
