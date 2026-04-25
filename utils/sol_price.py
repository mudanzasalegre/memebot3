from __future__ import annotations

import logging
import os
import time
from typing import Optional, Tuple

import aiohttp


logger = logging.getLogger("sol_price")

_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
_TTL_OK = int(os.getenv("COINGECKO_SOL_TTL", "60"))
_TIMEOUT = float(os.getenv("COINGECKO_TIMEOUT", "6"))
try:
    _SOL_USD_OVERRIDE = float((os.getenv("SOL_USD_OVERRIDE", "") or "").strip())
except Exception:
    _SOL_USD_OVERRIDE = 0.0

_CACHE: Optional[Tuple[float, float]] = None
_LAST_GOOD_PRICE: Optional[float] = None


async def _fetch_sol_usd() -> Optional[float]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_COINGECKO_URL, timeout=_TIMEOUT) as resp:
                if resp.status != 200:
                    logger.warning("[sol_price] CoinGecko HTTP %s", resp.status)
                    return None
                data = await resp.json()
                return float(data["solana"]["usd"])
    except Exception as exc:
        logger.warning("[sol_price] Error solicitando precio SOL: %s", exc)
        return None


async def get_sol_usd() -> Optional[float]:
    global _CACHE, _LAST_GOOD_PRICE

    if _SOL_USD_OVERRIDE > 0:
        return float(_SOL_USD_OVERRIDE)

    now = time.time()
    if _CACHE and now < _CACHE[1]:
        return _CACHE[0] if _CACHE[0] > 0 else None

    price = await _fetch_sol_usd()
    if price is not None and price > 0:
        _CACHE = (float(price), now + _TTL_OK)
        _LAST_GOOD_PRICE = float(price)
        return float(price)

    if _LAST_GOOD_PRICE is not None and _LAST_GOOD_PRICE > 0:
        _CACHE = (float(_LAST_GOOD_PRICE), now + 10)
        return float(_LAST_GOOD_PRICE)

    _CACHE = (0.0, now + 10)
    return None


async def amount_sol_to_usd(amount_sol: float) -> Optional[float]:
    try:
        amount = float(amount_sol)
    except Exception:
        return None
    if amount <= 0:
        return 0.0
    sol_usd = await get_sol_usd()
    if sol_usd is None or sol_usd <= 0:
        return None
    return float(amount * sol_usd)


__all__ = ["get_sol_usd", "amount_sol_to_usd"]
