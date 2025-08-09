# memebot3/fetcher/geckoterminal.py
"""
Wrapper para la API pública de **GeckoTerminal** (CoinGecko DEX).

• Solo actúa como *fallback* cuando DexScreener y Birdeye no traen
  liquidez, precio, etc., y la variable de entorno `USE_GECKO_TERMINAL=true`.

• Devuelve un dict **normalizado** con las mismas claves que produce
  DexScreener (`sanitize_token_data` las reconoce al instante):
    {
        "price_usd":         float | None,
        "liquidity_usd":     float | None,
        "volume_24h_usd":    float | None,
        "market_cap_usd":    float | None,
    }

Mejoras 2025-08-09
──────────────────
• TTL adaptable para “sin datos”:
      GECKO_TTL_NIL_SHORT (def. 120 s) → 3 primeros fallos consecutivos.
      GECKO_TTL_NIL_MAX   (def. 600 s) → del 4º fallo en adelante.
• Rate-limit interno extra: 1 req/2 s máx. además del bucket global.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional, TypedDict, cast

import requests
from config.config import GECKO_API_URL
from utils.rate_limiter import GECKO_LIMITER
from utils.simple_cache import cache_get, cache_set

try:
    import aiohttp
except ImportError:                                   # pragma: no cover
    aiohttp = None  # type: ignore

logger = logging.getLogger(__name__)

# ────────────────────────── flags y constantes ──────────────────────────
USE_GECKO_TERMINAL = os.getenv("USE_GECKO_TERMINAL", "true").lower() == "true"
_BASE_URL = GECKO_API_URL.rstrip("/")

_TTL_NIL_SHORT = int(os.getenv("GECKO_TTL_NIL_SHORT", "120"))
_TTL_NIL_MAX = int(os.getenv("GECKO_TTL_NIL_MAX", "600"))
_SENTINEL_NIL = object()

# contador de fallos consecutivos
_fail_count: dict[str, int] = {}

# rate-limit interno extra (mínimo intervalo entre llamadas)
_last_call_ts = 0.0
_min_interval_s = 2.0  # 1 req/2 s → 30 req/min máx.

# ─────────────────────────────── tipos ──────────────────────────────────
class GTData(TypedDict, total=False):
    price_usd: float | None
    liquidity_usd: float | None
    volume_24h_usd: float | None
    market_cap_usd: float | None


# ─────────────────────── helpers internos ───────────────────────────────
def _throttle_internal() -> None:
    """Pausa si no ha pasado el intervalo mínimo desde la última llamada."""
    global _last_call_ts
    now = time.monotonic()
    delta = now - _last_call_ts
    if delta < _min_interval_s:
        time.sleep(_min_interval_s - delta)
    _last_call_ts = time.monotonic()


def _build_endpoint(network: str, address: str) -> str:
    network = network.lower()
    if network == "solana":
        return f"{_BASE_URL}/networks/solana/tokens/{address}"
    return f"{_BASE_URL}/networks/{network}/tokens/{address}"


def _parse_attributes(attrs: dict) -> GTData:
    def _to_float(val: str | float | None) -> Optional[float]:
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    price = (
        attrs.get("base_token_price_usd")
        or attrs.get("price_usd")
        or attrs.get("price_in_usd")
    )

    liquidity = (
        attrs.get("reserve_in_usd")
        or attrs.get("total_reserve_in_usd")
        or attrs.get("liquidity_in_usd")
    )

    mcap = attrs.get("market_cap_usd") or attrs.get("fdv_usd")

    vol_node = attrs.get("volume_usd")
    vol24 = vol_node.get("h24") if isinstance(vol_node, dict) else None

    return GTData(
        price_usd=_to_float(price),
        liquidity_usd=_to_float(liquidity),
        volume_24h_usd=_to_float(vol24),
        market_cap_usd=_to_float(mcap),
    )


# ────────────────────────── API pública (sync) ──────────────────────────
def get_token_data(
    network: str,
    address: str,
    session: Optional[requests.Session] = None,
    timeout: int = 5,
) -> Optional[GTData]:
    if not USE_GECKO_TERMINAL:
        return None

    ck = f"gt:{network}:{address}"
    hit = cache_get(ck)
    if hit is not None:
        return None if hit is _SENTINEL_NIL else hit

    _throttle_internal()
    _acquire_sync()

    url = _build_endpoint(network, address)
    headers = {"Accept": "application/json", "User-Agent": "memebot3/1.0"}
    sess = session or requests

    try:
        resp = sess.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 404:
            _register_fail(ck)
            return None
        resp.raise_for_status()
        attrs = resp.json()["data"]["attributes"]
    except Exception as exc:
        logger.warning("[GT] Error %s para %s…", exc, address[:6])
        _register_fail(ck)
        return None

    _reset_fail(ck)
    data = _parse_attributes(attrs)
    cache_set(ck, data, ttl=60)  # TTL corto para datos OK
    return data


# ────────────────────────── API pública (async) ─────────────────────────
async def get_token_data_async(
    network: str,
    address: str,
    session: Optional["aiohttp.ClientSession"] = None,
    timeout: int = 5,
) -> Optional[GTData]:
    if not USE_GECKO_TERMINAL:
        return None

    ck = f"gt:{network}:{address}"
    hit = cache_get(ck)
    if hit is not None:
        return None if hit is _SENTINEL_NIL else hit

    # ratelimit interno + bucket global
    global _last_call_ts
    now = time.monotonic()
    delta = now - _last_call_ts
    if delta < _min_interval_s:
        await asyncio.sleep(_min_interval_s - delta)
    _last_call_ts = time.monotonic()

    async with GECKO_LIMITER:
        url = _build_endpoint(network, address)
        headers = {"Accept": "application/json", "User-Agent": "memebot3/1.0"}

        async def _fetch(client: "aiohttp.ClientSession") -> Optional[dict]:
            try:
                async with client.get(url, headers=headers, timeout=timeout) as r:
                    if r.status == 404:
                        _register_fail(ck)
                        return None
                    r.raise_for_status()
                    return cast(dict, await r.json())
            except aiohttp.ClientError as exc:
                logger.warning("[GT] Error red %s", exc)
                _register_fail(ck)
                return None

        client = session or aiohttp.ClientSession()
        try:
            j = await _fetch(client)
        finally:
            if session is None:
                await client.close()

    if not j:
        return None

    try:
        attrs = j["data"]["attributes"]
    except KeyError:
        logger.warning("[GT] JSON sin 'attributes' para %s…", address[:6])
        _register_fail(ck)
        return None

    _reset_fail(ck)
    data = _parse_attributes(attrs)
    cache_set(ck, data, ttl=60)
    return data


# ───────────────────────── helpers de rate-limit sync ───────────────────
def _acquire_sync() -> None:
    while True:
        now = time.monotonic()
        if now - GECKO_LIMITER._last_reset >= GECKO_LIMITER.interval:      # noqa: SLF001
            GECKO_LIMITER._tokens = GECKO_LIMITER.max_calls                # noqa: SLF001
            GECKO_LIMITER._last_reset = now                                # noqa: SLF001
        if GECKO_LIMITER._tokens > 0:                                      # noqa: SLF001
            GECKO_LIMITER._tokens -= 1                                     # noqa: SLF001
            return
        time.sleep(GECKO_LIMITER._time_until_reset() + 0.01)               # noqa: SLF001


# ───────────────────────── control de fallos ────────────────────────────
def _register_fail(key: str) -> None:
    fails = _fail_count.get(key, 0) + 1
    _fail_count[key] = fails
    ttl = _TTL_NIL_MAX if fails >= 4 else _TTL_NIL_SHORT
    cache_set(key, _SENTINEL_NIL, ttl=ttl)
    logger.debug("[GT] %s → sin datos (TTL=%ss, fallos=%d)", key, ttl, fails)


def _reset_fail(key: str) -> None:
    _fail_count.pop(key, None)


__all__ = [
    "get_token_data",
    "get_token_data_async",
    "USE_GECKO_TERMINAL",
]
