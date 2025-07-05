# memebot3/fetcher/socials.py
"""
Comprobación de redes sociales vía DexScreener endpoint /token/solana/<mint>.

Marca `True` si existe al menos uno de:
    • twitterUrl
    • telegramUrl
    • discordUrl
    • website
Caché in-memory de 10 min para evitar rate-limit.
"""

from __future__ import annotations

import logging
from typing import Dict

import aiohttp

from config import DEX_API_BASE
from utils.simple_cache import cache_get, cache_set

log = logging.getLogger("socials")

BASE = DEX_API_BASE.rstrip("/")
TTL  = 600        # 10 min


def _ok(profile: Dict) -> bool:
    links = profile.get("links") or {}
    return any(links.get(k) for k in (
        "twitterUrl", "telegramUrl", "discordUrl", "website"
    ))


async def has_socials(address: str) -> bool:
    ck = f"social:{address}"
    if (hit := cache_get(ck)) is not None:
        return hit          # type: ignore [return-value]

    url = f"{BASE}/token/solana/{address}"
    try:
        async with aiohttp.ClientSession() as s, s.get(url, timeout=12) as r:
            if r.status != 200:
                cache_set(ck, False, ttl=TTL)
                return False
            profile = await r.json()
    except Exception as e:  # noqa: BLE001
        log.debug("[socials] %s → %s", address[:4], e)
        cache_set(ck, False, ttl=TTL)
        return False

    ok = _ok(profile)
    cache_set(ck, ok, ttl=TTL)
    return ok
