# memebot3/fetcher/dexscreener.py
"""
Fetcher DexScreener (async) con TTL-cache y back-off.

Cambios
───────
2025-08-24
• parse_iso_utc para fechas ISO y manejo seguro de enteros ms/seg (sin .replace).
• Coerción estricta a float en price/liquidity/volume/mcap y aplanado del tick.
• Exposición de price_native (si viene) y de txns_last_5m / txns_last_5m_sells.
• Aliases de compat: liquidity.usd, volume24h, fdv (desde market_cap_usd).

2025-08-17
• Endpoints actualizados a `latest/dex/...` (compat con base sin `/latest`).
• Se prioriza `latest/dex/tokens/{mint}` y `latest/dex/pairs/solana/{pair}`.
• Fallback `latest/dex/search?q=` si no sabemos si es mint o pair.
• Normalización: `address` SIEMPRE es el **mint** (baseToken.address).
  Se añade `pair_address` en el payload para no confundir.
• `volume_24h_usd` desde `volume.h24` (con fallbacks).
• Compat retro: añade alias `liquidity.usd`, `volume24h`, `fdv`.

2025-07-26
• Se añade extracción de **market_cap_usd** para filtros y features/ML.

2025-07-20
• Liquidez/volumen quedan en **np.nan** cuando la API no los trae aún
  (evita ceros “muertos” en columnas).
"""
from __future__ import annotations

import os
import asyncio
import datetime as dt
import logging
from typing import Dict, Optional, List, Any

import aiohttp
import numpy as np

from config import DEX_API_BASE
from utils.data_utils import sanitize_token_data
from utils.simple_cache import cache_get, cache_set
from utils.time import parse_iso_utc  # ← usar helper seguro para ISO

log = logging.getLogger("dexscreener")

# ───────────────────────── config / estado ─────────────────────────
DEX = DEX_API_BASE.rstrip("/")

_MAX_TRIES, _BACKOFF_START = 3, 1
_CACHE_TTL_OK = 120
_TTL_NIL_SHORT = int(os.getenv("DEXS_TTL_NIL_SHORT", "90"))
_TTL_NIL_MAX = int(os.getenv("DEXS_TTL_NIL_MAX", "600"))
_SENTINEL_NIL = object()

# contador de fallos consecutivos por token
_fail_count: dict[str, int] = {}

# ───────────────────────── helpers URL ───────────────────────────
def _u(*parts: str) -> str:
    """
    Une partes de URL evitando dobles // y permitiendo bases con/sin `/latest`.
    Uso: _u("latest/dex/tokens", mint)
    """
    return "/".join([DEX] + [p.strip("/") for p in parts if p])

# ───────────────────────── helpers HTTP ──────────────────────────
async def _fetch_json(url: str, sess: aiohttp.ClientSession, *, params: dict | None = None) -> Optional[dict]:
    backoff = _BACKOFF_START
    for attempt in range(_MAX_TRIES):
        try:
            async with sess.get(
                url,
                params=params,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (MemeBot3)", "Accept": "application/json"},
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
def _safe_float(val) -> float | None:
    try:
        if val is None:
            return None
        # strings tipo "1,234.56" → quitar separadores si vinieran
        if isinstance(val, str):
            val = val.replace(",", "")
        return float(val)
    except Exception:
        return None

def _parse_created_any(ts: int | float | str | None) -> Optional[dt.datetime]:
    """
    Devuelve datetime aware en UTC a partir de:
      • int/float (ms o s desde epoch)
      • str ISO-8601 (usa parse_iso_utc)
    """
    if ts is None:
        return None
    # numérico → ms o s
    if isinstance(ts, (int, float)):
        try:
            # heurística: > 10^12 → ms; > 10^10 → ms con decimales; sino s
            secs = float(ts) / 1000.0 if float(ts) > 1e11 else float(ts)
            return dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc)
        except Exception:
            return None
    # string ISO
    return parse_iso_utc(str(ts))

def _extract_price_fields(raw: dict) -> tuple[float | None, float | None]:
    """
    Extrae (price_usd, price_native) normalizados a float.
    DexScreener suele traer `priceUsd` y a veces `priceNative`.
    """
    p_usd = _safe_float(raw.get("priceUsd") or raw.get("price"))
    p_nat = _safe_float(raw.get("priceNative") or raw.get("priceSol") or raw.get("priceBase"))
    return p_usd, p_nat

def _extract_liquidity_usd(raw: dict, price_usd: float | None) -> float | None:
    """
    Liquidez USD desde:
      • liquidity.usd (preferente)
      • liquidityUsd / liquidityLockedUsd
      • (fallback) liquidityLocked * price_usd
    """
    liq = raw.get("liquidity")
    if isinstance(liq, dict) and "usd" in liq:
        v = _safe_float(liq.get("usd"))
        if v is not None:
            return v
    # variantes en flat
    for k in ("liquidityUsd", "liquidity_locked_usd", "liquidityLockedUsd", "liqLockedUsd"):
        v = _safe_float(raw.get(k))
        if v is not None:
            return v

    # tokens bloqueados × precio
    locked_tokens = raw.get("liqLocked") or raw.get("liquidityLocked")
    if locked_tokens is not None and price_usd is not None:
        lt = _safe_float(locked_tokens)
        if lt is not None:
            return lt * price_usd

    return None

def _extract_volume_24h(raw: dict) -> float | None:
    """
    Volumen USD 24h desde:
      • volume.h24 (o h24Usd)
      • volume.usd
      • fallbacks: volume24hUsd / volume24h
    """
    vol = None
    vol_dict = raw.get("volume")
    if isinstance(vol_dict, dict):
        vol = vol_dict.get("h24") or vol_dict.get("h24Usd") or vol_dict.get("usd")
    if vol is None:
        vol = raw.get("volume24hUsd") or raw.get("volume24h")
    return _safe_float(vol)

def _extract_market_cap(raw: dict) -> float | None:
    """
    FDV/MarketCap desde varias variantes comunes.
    """
    for k in (
        "marketCap",
        "fdv",
        "fullyDilutedValuation",
        "fullyDilutedMarketCap",
        "fdvUsd",
        "fully_diluted_valuation",
    ):
        v = _safe_float(raw.get(k))
        if v is not None:
            return v
    return None

def _pick_best_pair(pairs: List[dict]) -> Optional[dict]:
    """
    Elige el mejor par (Solana) priorizando mayor liquidez USD y volumen 24h.
    """
    if not pairs:
        return None

    # Filtra solana si hay etiqueta; si no, usa todos
    spairs = [p for p in pairs if (p.get("chainId") or p.get("chain")) == "solana"]
    if not spairs:
        spairs = pairs[:]

    def liq_usd(p: dict) -> float:
        liq = p.get("liquidity")
        if isinstance(liq, dict):
            v = _safe_float(liq.get("usd"))
            return v or 0.0
        return _safe_float(liq) or 0.0

    def vol_24h(p: dict) -> float:
        vdict = p.get("volume")
        if isinstance(vdict, dict):
            v = _safe_float(vdict.get("h24"))
            return v or 0.0
        return 0.0

    spairs.sort(key=lambda p: (liq_usd(p), vol_24h(p)), reverse=True)
    return spairs[0]

def _add_legacy_aliases(tok: dict) -> dict:
    """
    Inyecta:
      - liquidity.usd ← liquidity_usd (aunque sea np.nan)
      - volume24h    ← volume_24h_usd
      - fdv          ← market_cap_usd
    para compatibilidad con lectores antiguos.
    """
    out = dict(tok)
    liq_usd = out.get("liquidity_usd", np.nan)
    if "liquidity" not in out or not isinstance(out.get("liquidity"), dict):
        out["liquidity"] = {}
    out["liquidity"]["usd"] = liq_usd
    if "volume24h" not in out:
        out["volume24h"] = out.get("volume_24h_usd", np.nan)
    if "fdv" not in out:
        out["fdv"] = out.get("market_cap_usd", np.nan)
    return out

# ───────────────────────── normalización main ─────────────────────
def _norm_from_pair(raw_pair: dict) -> dict:
    """
    Normaliza un objeto "pair" de DexScreener a nuestro esquema estándar.
    address      → SIEMPRE **mint SPL** del baseToken
    pair_address → dirección del par (Raydium/Orca/etc.)
    """
    base = raw_pair.get("baseToken") if isinstance(raw_pair.get("baseToken"), dict) else {}
    base = base or {}
    mint = base.get("address") or raw_pair.get("tokenAddress")  # endpoints legacy
    pair_address = raw_pair.get("pairAddress") or raw_pair.get("address")

    created_raw = raw_pair.get("listedAt") or raw_pair.get("createdAt") or raw_pair.get("pairCreatedAt")
    created_dt = _parse_created_any(created_raw)

    price_usd, price_native = _extract_price_fields(raw_pair)
    liq_usd   = _extract_liquidity_usd(raw_pair, price_usd)
    vol_usd   = _extract_volume_24h(raw_pair)
    mcap_usd  = _extract_market_cap(raw_pair)

    # txns últimos 5m
    txns_m5 = (raw_pair.get("txns") or {}).get("m5") or {}
    buys_5m  = _safe_float(txns_m5.get("buys")) or 0.0
    sells_5m = _safe_float(txns_m5.get("sells")) or 0.0
    total_5m = (buys_5m or 0.0) + (sells_5m or 0.0)

    tok = {
        "address":        (str(mint).strip() if mint else None),  # ← MINT SPL ¡clave!
        "pair_address":   pair_address,
        "symbol":         (base.get("symbol") or raw_pair.get("symbol")),
        "created_at":     created_dt,
        "price_usd":      price_usd if price_usd is not None else np.nan,
        "price_native":   price_native if price_native is not None else np.nan,
        "liquidity_usd":  liq_usd   if liq_usd   is not None else np.nan,
        "volume_24h_usd": vol_usd   if vol_usd   is not None else np.nan,
        "market_cap_usd": mcap_usd  if mcap_usd  is not None else np.nan,
        # señales rápidas
        "txns_last_5m":        total_5m or 0.0,
        "txns_last_5m_sells":  sells_5m or 0.0,
        "holders": _safe_float(raw_pair.get("holders")),
        # Pasar algunos campos originales por compat/debug
        **raw_pair,
    }
    tok = sanitize_token_data(tok)
    tok = _add_legacy_aliases(tok)
    return tok

# ───────────────────────── API pública ────────────────────────────
async def get_pair(address: str) -> Optional[Dict[str, Any]]:
    ck = f"dex:{address}"
    hit = cache_get(ck)
    if hit is not None:
        return None if hit is _SENTINEL_NIL else hit

    async with aiohttp.ClientSession() as s:
        # ① tokens (mint → lista de pares)
        url_tokens = _u("latest/dex/tokens", address)
        raw_tok = await _fetch_json(url_tokens, s)
        if raw_tok:
            log.debug("[DEX] %s tokens→ %s", address[:6], list(raw_tok.keys())[:3])
        if isinstance(raw_tok, dict) and raw_tok.get("pairs"):
            pair = _pick_best_pair(raw_tok["pairs"])
            if pair:
                res = _norm_from_pair(pair)
                if res.get("address"):
                    log.debug("[DEX] %s ✅ tokens-hit (mint)", address[:6])
                    cache_set(ck, res, ttl=_CACHE_TTL_OK)
                    _fail_count.pop(address, None)
                    return res

        # ② pairs (pairAddress directo)
        url_pair = _u("latest/dex/pairs/solana", address)
        raw_pair = await _fetch_json(url_pair, s)
        if raw_pair:
            log.debug("[DEX] %s pairs→ %s", address[:6], list(raw_pair.keys())[:3])
        if isinstance(raw_pair, dict):
            if raw_pair.get("pair"):
                res = _norm_from_pair(raw_pair["pair"])
                if res.get("address"):
                    log.debug("[DEX] %s ✅ pair-hit (direct)", address[:6])
                    cache_set(ck, res, ttl=_CACHE_TTL_OK)
                    _fail_count.pop(address, None)
                    return res
            if raw_pair.get("pairs"):
                pair = _pick_best_pair(raw_pair["pairs"])
                if pair:
                    res = _norm_from_pair(pair)
                    if res.get("address"):
                        log.debug("[DEX] %s ✅ pair-hit (list)", address[:6])
                        cache_set(ck, res, ttl=_CACHE_TTL_OK)
                        _fail_count.pop(address, None)
                        return res

        # ③ fallback search
        url_search = _u("latest/dex/search")
        raw_search = await _fetch_json(url_search, s, params={"q": address})
        if raw_search:
            log.debug("[DEX] %s search→ %s", address[:6], list(raw_search.keys())[:3])
        if isinstance(raw_search, dict) and raw_search.get("pairs"):
            pair = _pick_best_pair(raw_search["pairs"])
            if pair:
                res = _norm_from_pair(pair)
                if res.get("address"):
                    log.debug("[DEX] %s ✅ search-hit", address[:6])
                    cache_set(ck, res, ttl=_CACHE_TTL_OK)
                    _fail_count.pop(address, None)
                    return res

    # si llega aquí, no hubo datos
    fails = _fail_count.get(address, 0) + 1
    _fail_count[address] = fails
    ttl = _TTL_NIL_MAX if fails >= 4 else _TTL_NIL_SHORT

    cache_set(ck, _SENTINEL_NIL, ttl=ttl)
    log.debug("[DEX] %s ❌ sin datos (TTL=%ss, fallos=%d)", address[:6], ttl, fails)
    return None
