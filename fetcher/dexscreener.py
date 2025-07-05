"""
Fetcher DexScreener (async) con TTL-cache y back-off.

Cambios 2025-06-22
──────────────────
• _norm() ahora contempla «listedAt» (epoch ms) → fecha correcta.
• Si la API devuelve dicts vacíos para liquidez/volumen los deja tal
  cual; el *sanitizer* se ocupa de convertirlos a 0.0.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Dict, Optional

import aiohttp
import dateutil.parser as dparser

from config import DEX_API_BASE
from utils.data_utils import sanitize_token_data
from utils.simple_cache import cache_get, cache_set

log = logging.getLogger("dexscreener")
DEX = DEX_API_BASE.rstrip("/")

# ───── parámetros de back-off / caché ──────────────────────────
_MAX_TRIES     = 3
_BACKOFF_START = 1           # s
_CACHE_TTL_OK  = 120         # hit
_CACHE_TTL_NIL  = 3600        # miss
_SENTINEL_NIL  = object()    # marca “no existe”

# ───── HTTP helper con reintentos ────────────────────────
async def _fetch_json(url: str, session: aiohttp.ClientSession) -> Optional[dict]:
    backoff = _BACKOFF_START
    for attempt in range(_MAX_TRIES):
        try:
            async with session.get(
                url,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            ) as r:
                if r.status == 404:
                    return None
                if r.status in {429, 500, 502, 503, 504}:
                    raise aiohttp.ClientResponseError(
                        r.request_info, (), status=r.status, message=str(r.status)
                    )
                r.raise_for_status()
                return await r.json()

        except Exception as exc:                          # noqa: BLE001
            log.debug("[DEX] %s (try %s/%s)", exc, attempt + 1, _MAX_TRIES)
            if attempt < _MAX_TRIES - 1:
                await asyncio.sleep(backoff)
                backoff *= 2

    return None

# ───────── normalizador campos DexScreener ────────────
def _parse_datetime(ts: int | str | None) -> Optional[dt.datetime]:
    """
    Epoch (ms) o ISO8601 → dt.datetime UTC.
    """
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            return dt.datetime.fromtimestamp(int(ts) / 1000, tz=dt.timezone.utc)
        return dparser.parse(ts).astimezone(dt.timezone.utc)
    except Exception:
        return None

def _first_list_key(raw: dict) -> Optional[list]:
    """
    DexScreener a veces envuelve los datos en un campo de clave variable.
    """
    for v in raw.values():
        if isinstance(v, list):
            return v
    return None

def _norm(raw: dict) -> dict:
    """
    Convierte la respuesta de DexScreener en un dict homogéneo.
    • created_at ← listedAt ▸ createdAt ▸ pairCreatedAt
    • liquidez / volumen se dejan como dict → los castea el sanitizer.
    """
    created = (
        raw.get("listedAt")
        or raw.get("createdAt")
        or raw.get("pairCreatedAt")
    )
    tok = {
        # — meta
        "address": raw.get("address")
                   or raw.get("pairAddress")
                   or raw.get("tokenAddress"),
        "symbol":  raw.get("baseToken", {}).get("symbol") or raw.get("symbol"),
        "created_at": _parse_datetime(created),

        # — métricas (pueden venir como dict)
        "price_usd": float(raw.get("priceUsd") or raw.get("price") or 0),
        "liquidity": raw.get("liquidity")       or raw.get("liquidityUsd")  or {},
        "volume":    raw.get("volume")          or raw.get("volume24hUsd")  or {},
        "volume24h": raw.get("volume24h")       or raw.get("volume24hUsd")  or {},
        "txns_last_5min": int(
            raw.get("txns", {}).get("m5", {}).get("buys", 0)
            or raw.get("txnsLast5m", 0)
        ),
        "txns_last_5min_sells": int(raw.get("txns", {}).get("m5", {}).get("sells", 0)),
        "holders": int(raw.get("holders") or 0),
        **raw,  # copia cruda por si la necesitas en otra parte
    }
    return sanitize_token_data(tok)  # ← casting / alias / trend

# ───── API pública ─────────────────────────────────────────────
async def get_pair(address: str) -> Optional[Dict]:
    """
    Devuelve un dict normalizado o `None`.  
    Hits → 2 min • Miss → 1 h (para evitar spam).
    """
    address = address.strip()
    ck = f"dex:{address}"
    hit = cache_get(ck)
    if hit is not None:
        return None if hit is _SENTINEL_NIL else hit

    endpoints = [
        f"{DEX}/pairs/solana/{address}",
        f"{DEX}/pair/solana/{address}",
        f"{DEX}/dex/pairs/solana/{address}",
        f"{DEX}/latest/dex/tokens/{address}",
    ]

    async with aiohttp.ClientSession() as s:
        for url in endpoints:
            raw = await _fetch_json(url, s)
            if not raw:
                continue

            if isinstance(raw, dict) and raw.get("pairs"):
                res = _norm(raw["pairs"][0])
            elif isinstance(raw, dict) and "pair" in raw:
                res = _norm(raw["pair"])
            elif isinstance(raw, dict) and raw.get("data"):
                res = _norm(raw["data"])
            elif isinstance(raw, list) and raw:
                res = _norm(raw[0])
            else:
                lst = _first_list_key(raw)
                res = _norm(lst[0]) if lst else None

            if res:
                cache_set(ck, res, ttl=_CACHE_TTL_OK)
                return res

    cache_set(ck, _SENTINEL_NIL, ttl=_CACHE_TTL_NIL)
    log.debug("[DEX] %s → sin datos", address[:6])
    return None
