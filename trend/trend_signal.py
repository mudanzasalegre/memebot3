"""
Calcula una señal de tendencia muy simple usando DexScreener (precio 5 min).

• TTL-cache 1 min para no golpear la API.
"""
from __future__ import annotations

import logging

from fetcher import dexscreener
from utils.simple_cache import cache_get, cache_set

log = logging.getLogger("trend")

_TTL = 60  # s


async def trend_signal(address: str) -> int:
    """
    Devuelve:
        1  → tendencia alcista (> +15 % últimos 5 min)
        0  → plano / indeterminado
       -1  → bajista (< –15 %)
    """
    if (hit := cache_get(f"trend:{address}")) is not None:
        return hit

    pair = await dexscreener.get_pair(address)
    if not pair:
        cache_set(f"trend:{address}", 0, ttl=_TTL)
        return 0

    pct_5m = pair.get("price_pct_5m", 0)
    if pct_5m is None:
        pct_5m = 0

    if pct_5m >= 15:
        signal = 1
    elif pct_5m <= -15:
        signal = -1
    else:
        signal = 0

    cache_set(f"trend:{address}", signal, ttl=_TTL)
    return signal
