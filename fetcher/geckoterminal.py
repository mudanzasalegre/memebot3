# memebot3/fetcher/geckoterminal.py
"""
Wrapper para la API pública de **GeckoTerminal** (CoinGecko DEX).

• Actúa como *fallback* cuando DexScreener y Birdeye no traen datos,
  y `USE_GECKO_TERMINAL=true`.

Devuelve un dict **normalizado** con el mismo esquema estándar:
    {
        "address":          <mint SPL>,
        "pair_address":     <pool/pair si se conoce, o None>,
        "symbol":           <str|None>,
        "created_at":       <datetime aware|None>,
        "price_usd":        float|np.nan,
        "liquidity_usd":    float|np.nan,
        "volume_24h_usd":   float|np.nan,
        "market_cap_usd":   float|np.nan,
        ... (campos originales útiles)
    }

Mejoras 2025-08-24
──────────────────
• Coerción a float y aplanado (priceUsd, liquidity.usd, volume.h24, fdv/mcap).
• Campos extra: address/symbol/created_at con parse_iso_utc + epoch s/ms.
• Aliases de compat: liquidity.usd, volume24h, fdv (desde market_cap_usd).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import Optional, TypedDict, cast, Any, Dict

import requests
import numpy as np

from config.config import GECKO_API_URL
from utils.rate_limiter import GECKO_LIMITER
from utils.simple_cache import cache_get, cache_set
from utils.solana_addr import normalize_mint
from utils.time import parse_iso_utc
from utils.data_utils import sanitize_token_data

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

logger = logging.getLogger("fetcher.geckoterminal")

# ────────────────────────── flags y constantes ──────────────────────────
# Soportamos tanto USE_GECKO_TERMININAL (con doble "N") como el correcto.
USE_GECKO_TERMINAL = os.getenv("USE_GECKO_TERMININAL", os.getenv("USE_GECKO_TERMINAL", "true")).lower() == "true"
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
    address: str | None
    pair_address: str | None
    symbol: str | None
    created_at: object  # datetime | None
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


def _to_float(val: Any) -> Optional[float]:
    try:
        if val is None:
            return None
        if isinstance(val, str):
            val = val.replace(",", "")
        x = float(val)
        return None if (isinstance(x, float) and math.isnan(x)) else x
    except (TypeError, ValueError):
        return None


def _epoch_to_dt(epoch: Any):
    """Convierte epoch s/ms → datetime UTC (aware)."""
    try:
        x = float(epoch)
        if x > 1e11:  # ms heurístico
            x = x / 1000.0
        from datetime import datetime, timezone
        return datetime.fromtimestamp(x, tz=timezone.utc)
    except Exception:
        return None


def _pick_created_at(attrs: dict):
    """
    Intenta elegir un timestamp razonable del payload de GT.
    """
    # Orden de preferencia (strings ISO)
    for k in ("created_at", "pool_created_at", "pair_created_at", "listed_at", "launched_at", "launch_date", "updated_at", "last_refreshed_at"):
        dt = parse_iso_utc(attrs.get(k))
        if dt:
            return dt
    # Epochs comunes
    for k in ("created_at_timestamp", "pool_created_at_timestamp", "pair_created_at_timestamp", "updated_at_timestamp"):
        dt = _epoch_to_dt(attrs.get(k))
        if dt:
            return dt
    return None


def _add_legacy_aliases(d: dict) -> dict:
    """
    Inyecta:
      - liquidity.usd ← liquidity_usd
      - volume24h    ← volume_24h_usd
      - fdv          ← market_cap_usd
    para compatibilidad con lectores antiguos.
    """
    tok = dict(d)  # copia superficial
    # Asegura 'liquidity' como dict
    if not isinstance(tok.get("liquidity"), dict):
        tok["liquidity"] = {}
    liq = _to_float(tok.get("liquidity_usd"))
    tok["liquidity"]["usd"] = liq if liq is not None else np.nan

    vol24 = _to_float(tok.get("volume_24h_usd"))
    tok.setdefault("volume24h", vol24 if vol24 is not None else np.nan)

    mcap = _to_float(tok.get("market_cap_usd"))
    tok.setdefault("fdv", mcap if mcap is not None else np.nan)

    return tok


def _normalize_attributes(addr: str, attrs: dict) -> Dict[str, Any]:
    """
    Aplana y normaliza los atributos de GT al esquema estándar.
    """
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

    symbol = (
        attrs.get("symbol")
        or attrs.get("token_symbol")
        or attrs.get("base_token_symbol")
        or attrs.get("name")
    )

    created_at = _pick_created_at(attrs)

    # Calcula una sola vez para no invocar _to_float dos veces
    _price_f = _to_float(price)
    _liq_f   = _to_float(liquidity)
    _vol_f   = _to_float(vol24)
    _mcap_f  = _to_float(mcap)

    # IMPORTANTE: primero los atributos crudos y DESPUÉS los normalizados,
    # para que los floats normalizados sobreescriban posibles strings crudos.
    tok_raw_first: Dict[str, Any] = {
        **attrs,  # crudo de la API (puede contener strings y anidados)
        "address":        addr,
        "pair_address":   attrs.get("pool_address") or attrs.get("pair_address") or None,
        "symbol":         symbol,
        "created_at":     created_at,
        "price_usd":      _price_f if _price_f is not None else np.nan,
        "liquidity_usd":  _liq_f   if _liq_f   is not None else np.nan,
        "volume_24h_usd": _vol_f   if _vol_f   is not None else np.nan,
        "market_cap_usd": _mcap_f  if _mcap_f  is not None else np.nan,
    }

    tok = sanitize_token_data(tok_raw_first)
    tok = _add_legacy_aliases(tok)
    return tok


# ────────────────────────── API pública (sync) ──────────────────────────
def get_token_data(
    network: str,
    address: str,
    session: Optional[requests.Session] = None,
    timeout: int = 5,
) -> Optional[dict]:
    if not USE_GECKO_TERMINAL:
        return None

    # Normaliza el mint (quita 'pump', valida longitud/no-0x)
    addr = normalize_mint(address)
    if not addr:
        logger.warning("[GT] address inválido (no mint SPL): %r", address)
        return None

    ck = f"gt:{network}:{addr}"
    hit = cache_get(ck)
    if hit is not None:
        return None if hit is _SENTINEL_NIL else hit

    _throttle_internal()
    _acquire_sync()

    url = _build_endpoint(network, addr)
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
        logger.warning("[GT] Error %s para %s…", exc, addr[:6])
        _register_fail(ck)
        return None

    _reset_fail(ck)
    tok = _normalize_attributes(addr, attrs)
    try:
        # Casteo defensivo como en la versión async
        _p = tok.get("price_usd")
        _l = tok.get("liquidity_usd")
        _v = tok.get("volume_24h_usd")
        logger.debug(
            "[GT] %s | price %.6g liq %.0f vol24h %.0f",
            addr[:4],
            float(_p) if _p is not None else float("nan"),
            float(_l) if _l is not None else float("nan"),
            float(_v) if _v is not None else float("nan"),
        )
    except Exception:
        pass
    cache_set(ck, tok, ttl=60)  # TTL corto para datos OK
    return tok


# ────────────────────────── API pública (async) ─────────────────────────
async def get_token_data_async(
    network: str,
    address: str,
    session: Optional["aiohttp.ClientSession"] = None,
    timeout: int = 5,
) -> Optional[dict]:
    if not USE_GECKO_TERMINAL:
        return None

    # Normaliza el mint (quita 'pump', valida longitud/no-0x)
    addr = normalize_mint(address)
    if not addr:
        logger.warning("[GT] address inválido (no mint SPL): %r", address)
        return None

    ck = f"gt:{network}:{addr}"
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
        url = _build_endpoint(network, addr)
        headers = {"Accept": "application/json", "User-Agent": "memebot3/1.0"}

        async def _fetch(client: "aiohttp.ClientSession") -> Optional[dict]:
            try:
                async with client.get(url, headers=headers, timeout=timeout) as r:
                    if r.status == 404:
                        _register_fail(ck)
                        return None
                    r.raise_for_status()
                    return cast(dict, await r.json())
            except Exception as exc:  # aiohttp.ClientError y otros
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
        logger.warning("[GT] JSON sin 'attributes' para %s…", addr[:6])
        _register_fail(ck)
        return None

    _reset_fail(ck)
    tok = _normalize_attributes(addr, attrs)
    try:
        logger.debug(
            "[GT] %s | price %.6g liq %.0f vol24h %.0f",
            addr[:4],
            float(tok.get("price_usd")) if tok.get("price_usd") is not None else float("nan"),
            float(tok.get("liquidity_usd")) if tok.get("liquidity_usd") is not None else float("nan"),
            float(tok.get("volume_24h_usd")) if tok.get("volume_24h_usd") is not None else float("nan"),
        )
    except Exception:
        pass
    cache_set(ck, tok, ttl=60)
    return tok


# ───────────────────────── helpers de rate-limit sync ───────────────────
def _acquire_sync() -> None:
    while True:
        now = time.monotonic()
        # noqa: SLF001 por uso de atributos "privados" del limiter controlado
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
