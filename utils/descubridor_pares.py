"""
Scraper ultra-ligero de DexScreener que lista los *mint-addresses*
más recientes en Solana.

* Devuelve edad = 0.0 si no hay timestamp (deja que el filtro final decida).
* Filtra sólo por antigüedad aquí; el resto de reglas están en analytics.filters.
* NUEVO → CFG.MAX_CANDIDATES : recorta la lista para no procesar más de N tokens.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import List

import aiohttp

from utils.time import utc_now
from config import DEX_API_BASE, MAX_AGE_DAYS        # alias retro-compat
from config.config import CFG                        # ← incluye MAX_CANDIDATES

log = logging.getLogger("descubridor")
log.setLevel(logging.DEBUG)

# ───────────────────────── endpoints ──────────────────────────
DEX = DEX_API_BASE.rstrip("/")
URLS = [
    f"{DEX}/token-profiles/latest/v1?chainId=solana&limit=500",
    f"{DEX}/latest/dex/pairs/solana?limit=500",
]

# ───────────────────────── helpers ────────────────────────────
async def _json(s: aiohttp.ClientSession, url: str):
    try:
        async with s.get(
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        ) as r:
            if r.status == 404:
                return None
            r.raise_for_status()
            return await r.json()
    except Exception:           # noqa: BLE001
        return None


def _items(raw) -> list:
    """DexScreener suele envolver la lista en distintas claves."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for k in ("pairs", "tokens", "dexTokens", "data"):
            if isinstance(raw.get(k), list):
                return raw[k]
    return []


def _calc_age_days(tok: dict) -> float:
    """Calcula edad en días a partir de varios posibles campos timestamp."""
    ts_any = (
        tok.get("listedAt")
        or tok.get("createdAt")
        or tok.get("pairCreatedAt")
    )
    if ts_any is not None:
        try:
            ts = float(ts_any)
            if ts > 1e12:       # epoch ms → s
                ts /= 1000.0
            created = dt.datetime.utcfromtimestamp(ts)
            return (utc_now() - created).total_seconds() / 86400
        except Exception:       # noqa: BLE001
            pass

    # Si el endpoint ya trae la edad:
    for fld in ("ageDays", "age"):
        try:
            return float(tok.get(fld))
        except Exception:       # noqa: BLE001
            continue

    # Sin dato → asumimos 0 d para no filtrar aquí
    return 0.0


# ───────────────────────── API pública ─────────────────────────
async def fetch_candidate_pairs() -> List[str]:
    """Devuelve mint-addresses con edad ≤ MAX_AGE_DAYS (y ≤ MAX_CANDIDATES)."""
    async with aiohttp.ClientSession() as s:
        raw = None
        for u in URLS:
            raw = await _json(s, u)
            if raw:
                log.debug("DexScreener OK → %s", u.split(DEX)[1])
                break
        if not raw:
            log.error("DexScreener: ningún endpoint disponible")
            return []

    out: list[str] = []
    for t in _items(raw):
        addr = (
            t.get("tokenAddress")
            or t.get("baseToken", {}).get("address")
            or t.get("address")
        )
        if not addr:
            continue

        age = _calc_age_days(t)
        log.debug("⏱ %s age=%.1f", addr[:4], age)

        if age <= MAX_AGE_DAYS:
            out.append(addr)

        # NUEVO: recorta si se alcanzó el tope
        if CFG.MAX_CANDIDATES and len(out) >= CFG.MAX_CANDIDATES:
            break

    # elimina duplicados preservando orden
    out = list(dict.fromkeys(out))
    log.info("Descubridor: %s candidatos", len(out))
    return out


# ——— CLI rápido ———
if __name__ == "__main__":          # pragma: no cover
    async def _demo():
        lst = await fetch_candidate_pairs()
        print(len(lst), lst[:10])
    asyncio.run(_demo())
