# memebot3/fetcher/birdeye.py
"""
Fetcher para Birdeye (https://birdeye.so)

• FREE tier ≈ 1 req/s (60 RPM) – throttle cooperativo.
• Expone:
      get_token_info(address)   → métricas del token (normalizadas)
      get_pool_info(address)    → métricas del pool (normalizadas)

Devolvemos SIEMPRE un dict con **claves normalizadas**:
    address, pair_address?, symbol, created_at,
    price_usd, liquidity_usd, volume_24h_usd, market_cap_usd,
y mantenemos los campos originales recibidos de Birdeye (flatten) en el
mismo dict para compat y depuración.

Mejoras 2025-08-24
──────────────────
• Coerción estricta a float y aplanado de price/liquidity/volume/fdv.
• parse_iso_utc para fechas ISO + soporte unix epoch (s/ms).
• Aliases de compat: liquidity.usd, volume24h, fdv (desde market_cap_usd).

Mejoras previas
───────────────
• TTL adaptable para “sin datos”: BIRDEYE_TTL_NIL_SHORT / BIRDEYE_TTL_NIL_MAX.
• Normalización de mints (quita sufijo 'pump', valida SPL) para evitar 404.
"""

from __future__ import annotations

import aiohttp
import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

import numpy as np

from utils.simple_cache import cache_get, cache_set
from utils.solana_addr import normalize_mint
from utils.time import parse_iso_utc
from utils.data_utils import sanitize_token_data

# ───────────────────────── Config / constantes ──────────────────────────
_API_KEY:   Optional[str] = os.getenv("BIRDEYE_API_KEY")
_BASE_URL:  str           = "https://public-api.birdeye.so/public"

# Límite de peticiones por minuto (default 60 → 1 RPS)
_RPM:          int   = max(int(os.getenv("BIRDEYE_RPM", "60")), 1)
_MIN_INTERVAL: float = 60.0 / _RPM          # seg. entre llamadas

# TTL adaptativo para “sin datos”
_TTL_NIL_SHORT = int(os.getenv("BIRDEYE_TTL_NIL_SHORT", "90"))
_TTL_NIL_MAX   = int(os.getenv("BIRDEYE_TTL_NIL_MAX", "300"))
_SENTINEL_NIL  = object()

# Endpoints Birdeye (token / pool)
_TOKEN_EP: str = "/token/{addr}"
_POOL_EP:  str = "/pool/{addr}"

log = logging.getLogger("birdeye")

# rate-limit cooperativo (global en proceso)
_last_call_ts: float         = 0.0
_lock:          asyncio.Lock = asyncio.Lock()

# contador de fallos consecutivos
_fail_count: dict[str, int] = {}


# ───────────────────────── Helpers genéricos ─────────────────────────────
async def _throttle() -> None:
    """Enforce RPM; bloquea si la última llamada es muy reciente."""
    global _last_call_ts
    async with _lock:
        elapsed   = time.monotonic() - _last_call_ts
        wait_for  = _MIN_INTERVAL - elapsed
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        _last_call_ts = time.monotonic()


def _register_fail(key: str) -> None:
    fails = _fail_count.get(key, 0) + 1
    _fail_count[key] = fails
    ttl = _TTL_NIL_MAX if fails >= 4 else _TTL_NIL_SHORT
    cache_set(key, _SENTINEL_NIL, ttl=ttl)
    log.debug("[birdeye] %s → sin datos (TTL=%ss, fallos=%d)", key, ttl, fails)


def _reset_fail(key: str) -> None:
    _fail_count.pop(key, None)


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        if isinstance(v, str):
            v = v.replace(",", "")
        return float(v)
    except Exception:
        return None


def _epoch_to_dt(epoch: Any) -> Optional["datetime"]:
    """Convierte epoch s/ms → datetime UTC (aware)."""
    try:
        x = float(epoch)
        # Heurística ms vs s
        if x > 1e11:
            x = x / 1000.0
        from datetime import datetime, timezone
        return datetime.fromtimestamp(x, tz=timezone.utc)
    except Exception:
        return None


def _add_legacy_aliases(tok: dict) -> dict:
    """
    Aliases de compatibilidad:
      - liquidity.usd ← liquidity_usd (aunque sea np.nan)
      - volume24h    ← volume_24h_usd
      - fdv          ← market_cap_usd
    """
    out = dict(tok)
    liq_usd = out.get("liquidity_usd", np.nan)
    out.setdefault("liquidity", {})
    if not isinstance(out["liquidity"], dict):
        out["liquidity"] = {}
    out["liquidity"]["usd"] = liq_usd
    out.setdefault("volume24h", out.get("volume_24h_usd", np.nan))
    out.setdefault("fdv", out.get("market_cap_usd", np.nan))
    return out


# ───────────────────────── HTTP ───────────────────────────────────────────
async def _fetch(endpoint: str, cache_key: str) -> Dict[str, Any] | None:
    """
    GET <BASE_URL><endpoint> con cabecera Authorization.

    Devuelve el ``dict`` contenido en ``"data"`` o None.
    Usa TTL adaptable en caso de NIL para controlar reintentos.
    """
    if not _API_KEY:
        log.debug("[birdeye] desactivado – no hay API key")
        return None

    # cache hit
    hit = cache_get(cache_key)
    if hit is not None:
        return None if hit is _SENTINEL_NIL else hit

    await _throttle()

    url     = f"{_BASE_URL}{endpoint}"
    headers = {"Authorization": f"Bearer {_API_KEY}"}

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers=headers) as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    data = payload.get("data") or {}
                    _reset_fail(cache_key)
                    cache_set(cache_key, data, ttl=60)  # TTL corto para datos OK
                    return data
                log.debug("[birdeye] %s → HTTP %s", endpoint, resp.status)
    except Exception as exc:
        log.debug("[birdeye] request error %s → %s", endpoint, exc)

    _register_fail(cache_key)
    return None


# ───────────────────────── Normalizadores ─────────────────────────────────
def _normalize_token_payload(addr: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Aplana y normaliza la respuesta de /token/{addr} a nuestro esquema.
    Conserva además los campos originales en el mismo dict.
    """
    # Campos típicos (varían según versión de la API pública)
    price_usd = (
        _safe_float(raw.get("priceUsd"))
        or _safe_float(raw.get("price"))
        or _safe_float((raw.get("priceInfo") or {}).get("priceUsd"))
    )
    liq_usd = (
        _safe_float(raw.get("liquidityUsd"))
        or _safe_float((raw.get("liquidity") or {}).get("usd"))
        or _safe_float(raw.get("tvlUsd"))  # a veces lo llaman TVL
    )
    vol_24h = (
        _safe_float(raw.get("volume24hUsd"))
        or _safe_float(raw.get("v24hUsd"))
        or _safe_float((raw.get("volume") or {}).get("h24"))
        or _safe_float((raw.get("volume") or {}).get("usd"))
    )
    mcap_usd = (
        _safe_float(raw.get("fdv"))
        or _safe_float(raw.get("fdvUsd"))
        or _safe_float(raw.get("marketCap"))
        or _safe_float(raw.get("marketCapUsd"))
    )

    # Fecha de referencia (preferimos created; si no hay, última actualización)
    created_at = (
        parse_iso_utc(raw.get("createdAt"))
        or parse_iso_utc(raw.get("createTime"))
        or _epoch_to_dt(raw.get("createUnixTime"))
        or _epoch_to_dt(raw.get("updateUnixTime"))
    )

    out = {
        "address":        addr,
        "pair_address":   None,
        "symbol":         raw.get("symbol") or raw.get("baseSymbol") or raw.get("name"),
        "created_at":     created_at,
        "price_usd":      price_usd if price_usd is not None else np.nan,
        "liquidity_usd":  liq_usd   if liq_usd   is not None else np.nan,
        "volume_24h_usd": vol_24h   if vol_24h   is not None else np.nan,
        "market_cap_usd": mcap_usd  if mcap_usd  is not None else np.nan,
        # copia de algunos originales más frecuentes para depurar
        **raw,
    }
    out = sanitize_token_data(out)
    out = _add_legacy_aliases(out)
    return out


def _normalize_pool_payload(addr: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Aplana y normaliza la respuesta de /pool/{addr}. En pools,
    TVL suele equivaler a liquidez útil para nuestros filtros.
    """
    price_usd = (
        _safe_float(raw.get("priceUsd"))
        or _safe_float(raw.get("price"))
    )
    liq_usd = (
        _safe_float(raw.get("tvlUsd"))
        or _safe_float((raw.get("liquidity") or {}).get("usd"))
        or _safe_float(raw.get("liquidityUsd"))
    )
    vol_24h = (
        _safe_float(raw.get("volume24hUsd"))
        or _safe_float((raw.get("volume") or {}).get("h24"))
        or _safe_float((raw.get("volume") or {}).get("usd"))
    )
    mcap_usd = (
        _safe_float(raw.get("fdv"))
        or _safe_float(raw.get("marketCap"))
        or _safe_float(raw.get("fdvUsd"))
    )

    created_at = (
        parse_iso_utc(raw.get("createdAt"))
        or _epoch_to_dt(raw.get("createUnixTime"))
        or _epoch_to_dt(raw.get("updateUnixTime"))
    )

    out = {
        "address":        raw.get("baseMint") or raw.get("baseToken") or addr,
        "pair_address":   addr,
        "symbol":         raw.get("symbol") or raw.get("poolSymbol") or raw.get("name"),
        "created_at":     created_at,
        "price_usd":      price_usd if price_usd is not None else np.nan,
        "liquidity_usd":  liq_usd   if liq_usd   is not None else np.nan,
        "volume_24h_usd": vol_24h   if vol_24h   is not None else np.nan,
        "market_cap_usd": mcap_usd  if mcap_usd  is not None else np.nan,
        **raw,
    }
    out = sanitize_token_data(out)
    out = _add_legacy_aliases(out)
    return out


# ───────────────────────── API pública ────────────────────────────────────
async def get_token_info(address: str) -> Dict[str, Any] | None:
    """
    ``/token/{address}``   – precio, liquidez, mcap, volumen 24h…

    Normalizamos la dirección (quita sufijo 'pump' y valida mint SPL)
    antes de llamar al endpoint para evitar 404 y spam de reintentos.
    """
    addr = normalize_mint(address)
    if not addr:
        log.warning("[birdeye] address inválido (no mint SPL): %r", address)
        return None

    key = f"be:token:{addr}"
    data = await _fetch(_TOKEN_EP.format(addr=addr), key)
    if not data:
        return None

    out = _normalize_token_payload(addr, data)
    try:
        log.debug(
            "[birdeye] token %s | price %.6g  liq %.0f  vol24h %.0f  fdv %.0f",
            addr[:4], out.get("price_usd"), out.get("liquidity_usd"),
            out.get("volume_24h_usd"), out.get("market_cap_usd"),
        )
    except Exception:
        pass
    return out


async def get_pool_info(address: str) -> Dict[str, Any] | None:
    """
    ``/pool/{address}``    – stats de pool (TVL, volumen, fees, APR…)

    Nota: si tu flujo espera *pool address* real y no mint, puedes retirar la
    normalización y dejar que data_utils gestione advertencias. Dado tu log actual,
    mantenemos el mismo guardarraíl para evitar llamadas /pool/<mint>pump.
    """
    addr = normalize_mint(address)
    if not addr:
        log.warning("[birdeye] pool inválido (no mint SPL): %r", address)
        return None

    key = f"be:pool:{addr}"
    data = await _fetch(_POOL_EP.format(addr=addr), key)
    if not data:
        return None

    out = _normalize_pool_payload(addr, data)
    try:
        log.debug(
            "[birdeye] pool  %s | tvl/liquidity %.0f  vol24h %.0f",
            addr[:4], out.get("liquidity_usd"), out.get("volume_24h_usd"),
        )
    except Exception:
        pass
    return out


__all__ = [
    "get_token_info",
    "get_pool_info",
]
