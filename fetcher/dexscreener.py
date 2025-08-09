# memebot3/fetcher/dexscreener.py
"""
Fetcher DexScreener (async) con TTL-cache y back-off.

Cambios
───────
2025-07-20
• Liquidez y volumen ahora quedan en **np.nan** cuando la API no los trae
  en los primeros minutos (evita columnas 0 “muertas”).

2025-07-26
• Se añade extracción de **market_cap_usd** para poder filtrar por rango
  de capitalización y alimentar features/ML.

2025-08-09
• TTL de “sin datos” adaptable:
    - Corto (DEXS_TTL_NIL_SHORT) para los primeros 3 fallos.
    - Largo (DEXS_TTL_NIL_MAX) a partir del 4º fallo consecutivo.
  Configurable desde `.env`.
"""
from __future__ import annotations

import os
import asyncio
import datetime as dt
import logging
from typing import Dict, Optional

import aiohttp
import dateutil.parser as dparser
import numpy as np

from config import DEX_API_BASE
import utils
from utils.data_utils import sanitize_token_data
from utils.simple_cache import cache_get, cache_set

log = logging.getLogger("dexscreener")
DEX = DEX_API_BASE.rstrip("/")

_MAX_TRIES, _BACKOFF_START = 3, 1
_CACHE_TTL_OK = 120
_TTL_NIL_SHORT = int(os.getenv("DEXS_TTL_NIL_SHORT", "90"))
_TTL_NIL_MAX = int(os.getenv("DEXS_TTL_NIL_MAX", "600"))
_SENTINEL_NIL = object()

# contador de fallos consecutivos por token
_fail_count: dict[str, int] = {}

# ───────────────────────── helpers HTTP ──────────────────────────
async def _fetch_json(url: str, sess: aiohttp.ClientSession) -> Optional[dict]:
    backoff = _BACKOFF_START
    for attempt in range(_MAX_TRIES):
        try:
            async with sess.get(
                url,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            ) as r:
                if r.status == 404:
                    return None
                if r.status in {429, 500, 502, 503, 504}:
                    raise aiohttp.ClientResponseError(r.request_info, (), status=r.status)
                r.raise_for_status()
                return await r.json()
        except Exception as exc:  # pragma: no cover
            log.debug("[DEX] %s (try %s/%s)", exc, attempt + 1, _MAX_TRIES)
            if attempt < _MAX_TRIES - 1:
                await asyncio.sleep(backoff)
                backoff *= 2
    return None

# ───────────────────────── helpers parsing ───────────────────────
def _parse_datetime(ts: int | str | None) -> Optional[dt.datetime]:
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            return dt.datetime.fromtimestamp(int(ts) / 1000, tz=dt.timezone.utc)
        return dparser.parse(ts).astimezone(dt.timezone.utc)
    except Exception:  # pragma: no cover
        return None

def _first_list_key(raw: dict) -> Optional[list]:
    for v in raw.values():
        if isinstance(v, list):
            return v
    return None

# ───────────────────────── normalización numérica ────────────────
def _safe_float(val) -> float | None:
    try:
        return float(val)
    except Exception:
        return None

def _extract_liquidity_usd(raw: dict, price_usd: float | None) -> float | None:
    liq = raw.get("liquidity") or raw.get("liquidityUsd") or {}
    if isinstance(liq, (int, float)):
        return _safe_float(liq)
    if isinstance(liq, dict) and "usd" in liq:
        return _safe_float(liq["usd"])

    locked_usd = raw.get("liquidityLockedUsd") or raw.get("liqLockedUsd")
    if locked_usd:
        return _safe_float(locked_usd)

    locked_tokens = raw.get("liqLocked") or raw.get("liquidityLocked")
    if locked_tokens and price_usd:
        return _safe_float(locked_tokens) * price_usd
    return None

def _extract_volume_24h(raw: dict) -> float | None:
    vol = (
        raw.get("volume24hUsd")
        or raw.get("volume24h")
        or raw.get("volume", {}).get("usd")
        or raw.get("volume24h")
    )
    return _safe_float(vol)

def _extract_market_cap(raw: dict) -> float | None:
    cap = (
        raw.get("marketCap")
        or raw.get("fdv")
        or raw.get("fullyDilutedValuation")
        or raw.get("fullyDilutedMarketCap")
        or raw.get("fdvUsd")
        or raw.get("fully_diluted_valuation")
    )
    return _safe_float(cap)

# ───────────────────────── normalización main ─────────────────────
def _norm(raw: dict) -> dict:
    created = raw.get("listedAt") or raw.get("createdAt") or raw.get("pairCreatedAt")

    price_usd = _safe_float(raw.get("priceUsd") or raw.get("price"))
    liq_usd   = _extract_liquidity_usd(raw, price_usd)
    vol_usd   = _extract_volume_24h(raw)
    mcap_usd  = _extract_market_cap(raw)

    tok = {
        "address":  raw.get("address") or raw.get("pairAddress") or raw.get("tokenAddress"),
        "symbol":   raw.get("baseToken", {}).get("symbol") or raw.get("symbol"),
        "created_at": _parse_datetime(created),
        "price_usd":       price_usd if price_usd is not None else np.nan,
        "liquidity_usd":   liq_usd   if liq_usd   is not None else np.nan,
        "volume_24h_usd":  vol_usd   if vol_usd   is not None else np.nan,
        "market_cap_usd":  mcap_usd  if mcap_usd  is not None else np.nan,
        "txns_last_5m": _safe_float(
            raw.get("txns", {}).get("m5", {}).get("buys")
            or raw.get("txnsLast5m")
        ),
        "holders": _safe_float(raw.get("holders")),
        **raw,
    }
    return sanitize_token_data(tok)

# ───────────────────────── API pública ────────────────────────────
async def get_pair(address: str) -> Optional[Dict]:
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
                _fail_count.pop(address, None)  # reset fallos si hay datos
                cache_set(ck, res, ttl=_CACHE_TTL_OK)
                return res

    # si llega aquí, no hubo datos
    fails = _fail_count.get(address, 0) + 1
    _fail_count[address] = fails
    ttl = _TTL_NIL_MAX if fails >= 4 else _TTL_NIL_SHORT

    cache_set(ck, _SENTINEL_NIL, ttl=ttl)
    log.debug("[DEX] %s → sin datos (TTL=%ss, fallos=%d)", address[:6], ttl, fails)
    return None
