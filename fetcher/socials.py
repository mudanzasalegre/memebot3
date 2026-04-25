from __future__ import annotations

import logging
from typing import Dict, Optional

import aiohttp

from config import DEX_API_BASE
from utils.simple_cache import cache_get, cache_set

log = logging.getLogger("socials")

BASE = DEX_API_BASE.rstrip("/")
TTL = 600


def _ok(profile: Dict) -> bool:
    links = profile.get("links") or {}
    return any(links.get(key) for key in ("twitterUrl", "telegramUrl", "discordUrl", "website"))


async def has_socials(address: str) -> Optional[bool]:
    """
    True  -> hay socials
    False -> se consulto bien y no hay socials
    None  -> no pudimos determinarlo
    """
    ck = f"social:{address}"
    if (hit := cache_get(ck)) is not None:
        return hit  # type: ignore[return-value]

    url = f"{BASE}/token/solana/{address}"
    try:
        async with aiohttp.ClientSession() as sess, sess.get(url, timeout=12) as resp:
            if resp.status != 200:
                cache_set(ck, None, ttl=TTL // 2)
                return None
            profile = await resp.json()
    except Exception as exc:  # noqa: BLE001
        log.debug("[socials] %s -> %s", address[:4], exc)
        cache_set(ck, None, ttl=TTL // 2)
        return None

    ok = _ok(profile)
    cache_set(ck, ok, ttl=TTL)
    return ok
