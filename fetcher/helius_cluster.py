"""
Detecta concentración de supply usando Helius RPC con back-off y caché.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

import aiohttp

from config import HELIUS_RPC_URL, HELIUS_API_KEY
from utils.simple_cache import cache_get, cache_set

MAX_SHARE_TOP10 = 0.20
TOP_N = 10
TIMEOUT = 6
_MAX_TRIES = 3
_BACKOFF_START = 1  # s
_CACHE_TTL = 900   # 15 min

log = logging.getLogger("helius_cluster")


async def _rpc(method: str, params: list[Any]) -> Dict | None:
    if not HELIUS_API_KEY:
        return None

    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    backoff = _BACKOFF_START

    for attempt in range(_MAX_TRIES):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT)
            ) as s:
                async with s.post(HELIUS_RPC_URL, json=payload) as r:
                    if r.status in {429, 500, 502, 503, 504}:
                        raise aiohttp.ClientResponseError(r.request_info, (), status=r.status)
                    if r.status != 200:
                        log.debug("[Helius] %s", await r.text())
                        return None
                    data = await r.json()
                    return data.get("result")
        except Exception as e:  # noqa: BLE001
            log.debug("[Helius] %s (try %s/%s)", e, attempt + 1, _MAX_TRIES)
            if attempt < _MAX_TRIES - 1:
                await asyncio.sleep(backoff)
                backoff *= 2
    return None


async def suspicious_cluster(token_mint: str) -> bool:
    """
    True  → sospechoso, >20 % del supply en top-10
    False → distribución sana / error.
    """
    if not HELIUS_API_KEY:
        return False

    cache_key = f"helius:cluster:{token_mint}"
    if (hit := cache_get(cache_key)) is not None:
        return hit

    largest = await _rpc("getTokenLargestAccounts", [token_mint])
    supply  = await _rpc("getTokenSupply",          [token_mint])
    if not (largest and supply and "value" in largest and "value" in supply):
        cache_set(cache_key, False, ttl=_CACHE_TTL)
        return False

    try:
        total_supply = int(supply["value"]["amount"])
        top_sum = sum(int(acc["amount"]) for acc in largest["value"][:TOP_N])
    except Exception:  # noqa: BLE001
        cache_set(cache_key, False, ttl=_CACHE_TTL)
        return False

    share = top_sum / total_supply if total_supply else 0
    bad = share > MAX_SHARE_TOP10
    log.debug("[Helius] share_top10 %.2f%% → bad=%s", share * 100, bad)

    cache_set(cache_key, bad, ttl=_CACHE_TTL)
    return bad
