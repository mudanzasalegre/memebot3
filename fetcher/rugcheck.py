from __future__ import annotations

import logging

import aiohttp
import tenacity

from config import RUGCHECK_API_BASE, RUGCHECK_API_KEY
from utils.simple_cache import cache_get, cache_set

log = logging.getLogger("rugcheck")

_TTL_OK = 900
_TTL_MISS = 300
_SENTINEL_NIL = object()


if not (RUGCHECK_API_BASE and RUGCHECK_API_KEY):

    async def check_token(address: str) -> int | None:  # type: ignore
        return None

    log.warning("[RugCheck] Deshabilitado: faltan credenciales")

else:
    HEADERS = {"Authorization": f"Bearer {RUGCHECK_API_KEY}"}

    @tenacity.retry(wait=tenacity.wait_fixed(2), stop=tenacity.stop_after_attempt(3))
    async def _fetch_score(address: str) -> int | None:
        url = f"{RUGCHECK_API_BASE.rstrip('/')}/score/{address}"
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=HEADERS, timeout=10) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                data = await resp.json()
        score = data.get("score")
        return None if score is None else int(score)

    async def check_token(address: str) -> int | None:  # type: ignore
        ck = f"rug:{address}"
        hit = cache_get(ck)
        if hit is not None:
            return None if hit is _SENTINEL_NIL else hit  # type: ignore[return-value]

        try:
            score = await _fetch_score(address)
            if score is None:
                cache_set(ck, _SENTINEL_NIL, ttl=_TTL_MISS)
                return None
            value = max(int(score), 0)
            cache_set(ck, value, ttl=_TTL_OK)
            log.debug("[RugCheck] %s -> %s", address[:4], value)
            return value
        except Exception as exc:  # noqa: BLE001
            log.warning("[RugCheck] error %s", exc)
            cache_set(ck, _SENTINEL_NIL, ttl=_TTL_MISS)
            return None
