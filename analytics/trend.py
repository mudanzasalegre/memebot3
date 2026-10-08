from __future__ import annotations

import asyncio
import logging
import random
from typing import List, Literal

import aiohttp

from config import DEX_API_BASE
from utils.auxiliary_observation import trend_observation, checked_auxiliary_observation

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
    """Legacy diagnostic only; never called on the entry hot path.

    The public provider reference does not specify this chart route. Results
    here are not accepted as fresh executable entry observations.
    """
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


async def trend_signal(address: str, *, snapshot: dict | None = None) -> tuple[Literal["up", "down", "flat", "unknown"], bool]:
    """Compatibility API: explicitly a fresh m5 momentum proxy, not an EMA."""
    if snapshot is None:
        from fetcher import dexscreener
        snapshot = await dexscreener.get_pair(address, force_refresh=True)
    if not isinstance(snapshot, dict) or snapshot.get("address") != address:
        return "unknown", True
    record = checked_auxiliary_observation(trend_observation(snapshot), address, "trend")
    value = record["value"] if record is not None else None
    return {1: "up", -1: "down", 0: "flat"}.get(value, "unknown"), True


if __name__ == "__main__":  # pragma: no cover
    import sys

    test_addr = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "So11111111111111111111111111111111111111112"
    )
    print(asyncio.run(trend_signal(test_addr)))
