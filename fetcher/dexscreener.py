"""
Fetcher DexScreener (async) con TTL-cache y back-off.

2025-07-13
──────────
• No cambia la lógica: la ausencia de created_at
  la resuelve sanitize_token_data().
"""
from __future__ import annotations

import asyncio, datetime as dt, logging
from typing import Dict, Optional

import aiohttp, dateutil.parser as dparser

from config import DEX_API_BASE
from utils.data_utils import sanitize_token_data
from utils.simple_cache import cache_get, cache_set

log = logging.getLogger("dexscreener")
DEX = DEX_API_BASE.rstrip("/")

_MAX_TRIES, _BACKOFF_START = 3, 1
_CACHE_TTL_OK, _CACHE_TTL_NIL = 120, 3600
_SENTINEL_NIL = object()

async def _fetch_json(url: str, sess: aiohttp.ClientSession) -> Optional[dict]:
    backoff = _BACKOFF_START
    for attempt in range(_MAX_TRIES):
        try:
            async with sess.get(url, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0",
                                         "Accept": "application/json"}) as r:
                if r.status == 404:
                    return None
                if r.status in {429, 500, 502, 503, 504}:
                    raise aiohttp.ClientResponseError(r.request_info, (), status=r.status)
                r.raise_for_status()
                return await r.json()
        except Exception as exc:
            log.debug("[DEX] %s (try %s/%s)", exc, attempt + 1, _MAX_TRIES)
            if attempt < _MAX_TRIES - 1:
                await asyncio.sleep(backoff)
                backoff *= 2
    return None

def _parse_datetime(ts: int | str | None) -> Optional[dt.datetime]:
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            return dt.datetime.fromtimestamp(int(ts) / 1000, tz=dt.timezone.utc)
        return dparser.parse(ts).astimezone(dt.timezone.utc)
    except Exception:
        return None

def _first_list_key(raw: dict) -> Optional[list]:
    for v in raw.values():
        if isinstance(v, list):
            return v
    return None

def _norm(raw: dict) -> dict:
    created = raw.get("listedAt") or raw.get("createdAt") or raw.get("pairCreatedAt")
    tok = {
        "address": raw.get("address") or raw.get("pairAddress") or raw.get("tokenAddress"),
        "symbol":  raw.get("baseToken", {}).get("symbol") or raw.get("symbol"),
        "created_at": _parse_datetime(created),
        "price_usd": float(raw.get("priceUsd") or raw.get("price") or 0),
        "liquidity": raw.get("liquidity") or raw.get("liquidityUsd") or {},
        "volume":    raw.get("volume")    or raw.get("volume24hUsd") or {},
        "volume24h": raw.get("volume24h") or raw.get("volume24hUsd") or {},
        "txns_last_5min": int(
            raw.get("txns", {}).get("m5", {}).get("buys", 0)
            or raw.get("txnsLast5m", 0)
        ),
        "txns_last_5min_sells": int(raw.get("txns", {}).get("m5", {}).get("sells", 0)),
        "holders": int(raw.get("holders") or 0),
        **raw,
    }
    return sanitize_token_data(tok)

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
                cache_set(ck, res, ttl=_CACHE_TTL_OK)
                return res

    cache_set(ck, _SENTINEL_NIL, ttl=_CACHE_TTL_NIL)
    log.debug("[DEX] %s → sin datos", address[:6])
    return None
