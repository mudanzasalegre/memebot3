# memebot3/fetcher/rugcheck.py
"""
Wrapper de RugCheck con back-off y TTL-cache.
Devuelve un entero 0-100 (o 0 si no hay score).
"""
from __future__ import annotations

import logging
import aiohttp
import tenacity

from utils.simple_cache import cache_get, cache_set
from config import RUGCHECK_API_BASE, RUGCHECK_API_KEY

log = logging.getLogger("rugcheck")

# TTLs
_TTL_OK   = 900   # 15 min si hay score
_TTL_MISS = 300   # 5 min si 404 / error / no indexado

# ────────── stub si falta config ───────────────────────────────
if not (RUGCHECK_API_BASE and RUGCHECK_API_KEY):

    async def check_token(address: str) -> int:   # type: ignore
        return 0

    log.warning("[RugCheck] Deshabilitado: faltan credenciales")
# ───────────────────────────────────────────────────────────────
else:
    HEADERS = {"Authorization": f"Bearer {RUGCHECK_API_KEY}"}

    @tenacity.retry(wait=tenacity.wait_fixed(2),
                    stop=tenacity.stop_after_attempt(3))
    async def _fetch_score(address: str) -> int:
        url = f"{RUGCHECK_API_BASE.rstrip('/')}/score/{address}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=HEADERS, timeout=10) as r:
                if r.status == 404:
                    return -1            # aún no indexado
                r.raise_for_status()
                data = await r.json()
        return int(data.get("score", 0))

    async def check_token(address: str) -> int:   # type: ignore
        ck = f"rug:{address}"
        if (hit := cache_get(ck)) is not None:
            return hit                            # type: ignore

        try:
            score = await _fetch_score(address)
            ttl   = _TTL_MISS if score < 0 else _TTL_OK
            value = max(score, 0)                # -1 → 0
            cache_set(ck, value, ttl=ttl)
            log.debug("[RugCheck] %s → %s", address[:4], value)
            return value
        except Exception as exc:                 # noqa: BLE001
            log.warning("[RugCheck] error %s", exc)
            cache_set(ck, 0, ttl=_TTL_MISS)
            return 0
