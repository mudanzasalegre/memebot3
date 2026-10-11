# memebot3/fetcher/dexscreener.py
"""
Fetcher DexScreener (async) con TTL-cache y back-off.

Cambios
───────
2026-10-09
• Lookup actual token-pairs/v1/solana y fallback de Pair/search.
• chainId y base mint explícitos; pairCreatedAt es evento de par, no nacimiento.
• Metadata de fechas separada, nullable UTC, sin rejuvenecer una fecha conocida.

2025-09-15
• Normalización de `dexId`: se expone siempre `tok["dexId"]` **normalizado**
  (lowercase, sin espacios) y mapeado a familia (raydium/orca/meteora, etc.)
  para casar con DEX_WHITELIST. El valor normalizado prevalece sobre el crudo.

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
import math
import time
from copy import deepcopy
from typing import Dict, Optional, List, Any

import aiohttp
import numpy as np

from config import DEX_API_BASE
from utils.data_utils import sanitize_token_data
from utils.simple_cache import cache_get, cache_set, cache_delete
from utils.bounded_state import BoundedFailureCounter, bounded_int, increment_failure
from utils.solana_addr import normalize_mint
from analytics.token_time import parse_event_clock, venue_clock_snapshot
from utils.market_observation import MARKET_FIELDS, stamp_market_observation
from analytics.social_signal import social_signal_from_profile

log = logging.getLogger("dexscreener")

# ───────────────────────── config / estado ─────────────────────────
DEX = DEX_API_BASE.rstrip("/")

_MAX_TRIES, _BACKOFF_START = 3, 1
_CACHE_TTL_OK = 120
_TTL_NIL_SHORT = bounded_int(os.getenv("DEXS_TTL_NIL_SHORT", "90"), 90, 1, 3600)
_TTL_NIL_MAX = max(_TTL_NIL_SHORT, bounded_int(os.getenv("DEXS_TTL_NIL_MAX", "600"), 600, 1, 86400))
_SENTINEL_NIL = object()

# contador de fallos consecutivos por token
_fail_count = BoundedFailureCounter()

# ───────────────────────── helpers URL ───────────────────────────
def _u(*parts: str) -> str:
    """
    Une partes de URL evitando dobles // y permitiendo bases con/sin `/latest`.
    Uso: _u("token-pairs/v1/solana", mint)
    """
    base = DEX[:-7] if DEX.endswith("/latest") else DEX
    return "/".join([base] + [p.strip("/") for p in parts if p])

# ───────────────────────── helpers HTTP ──────────────────────────
async def _fetch_json(url: str, sess: aiohttp.ClientSession, *, params: dict | None = None) -> Any:
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
        if val is None or isinstance(val, bool):
            return None
        # strings tipo "1,234.56" → quitar separadores si vinieran
        if isinstance(val, str):
            val = val.replace(",", "")
        number = float(val)
        return number if math.isfinite(number) else None
    except Exception:
        return None

def _parse_created_any(ts: int | float | str | None) -> Optional[dt.datetime]:
    """Typed nullable UTC event diagnostic, not a mint-birth declaration."""
    return parse_event_clock(ts)

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

    spairs = [p for p in pairs if _valid_solana_pair(p)]
    if not spairs:
        return None

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

def _valid_solana_pair(pair: Any) -> bool:
    if (not isinstance(pair, dict) or not isinstance(pair.get("chainId"), str)
            or pair["chainId"].strip().lower() != "solana"):
        return False
    base = pair.get("baseToken")
    mint = base.get("address") if isinstance(base, dict) else None
    return isinstance(mint, str) and normalize_mint(mint) == mint.strip()


def _matching_pairs(pairs: list, address: str, *, allow_pair_address: bool = True) -> list[dict]:
    return [p for p in pairs if _valid_solana_pair(p)
            and (p["baseToken"]["address"].strip() == address
                 or allow_pair_address and isinstance(p.get("pairAddress"), str)
                    and p["pairAddress"].strip() == address)]


def _pair_rows(payload: Any) -> list[dict]:
    """Documented lists plus retained Pair response envelopes; no .keys crash."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("pairs"), list):
        return payload["pairs"]
    if isinstance(payload.get("pair"), dict):
        return [payload["pair"]]
    return []


def _add_legacy_aliases(tok: dict) -> dict:
    """
    Inyecta:
      - liquidity.usd ← liquidity_usd (aunque sea np.nan)
      - volume24h    ← volume_24h_usd
      - fdv          ← market_cap_usd
    para compatibilidad con lectores antiguos.
    """
    out = deepcopy(tok)
    liq_usd = out.get("liquidity_usd", np.nan)
    if "liquidity" not in out or not isinstance(out.get("liquidity"), dict):
        out["liquidity"] = {}
    out["liquidity"]["usd"] = liq_usd
    if "volume24h" not in out:
        out["volume24h"] = out.get("volume_24h_usd", np.nan)
    if "fdv" not in out:
        out["fdv"] = out.get("market_cap_usd", np.nan)
    return out

# ─────────────────────── normalización de dexId ───────────────────────
def _normalize_dex_id(raw_val: Any) -> Optional[str]:
    """
    Devuelve un dex_id **normalizado**:
      • lowercase
      • sin espacios (ni guiones/barras)
      • mapeado a familia: raydium / orca / meteora (y algunos extra)
    """
    if not raw_val:
        return None
    s0 = str(raw_val).strip().lower()
    if not s0:
        return None

    # Texto "sin espacios" (quitamos separadores comunes)
    s_clean = "".join(ch for ch in s0 if ch.isalnum())

    # Mapeo por familia (coincidencia por inclusión o exacta)
    family_map = {
        "raydium": "raydium",
        "raydiumamm": "raydium",
        "raydiumclmm": "raydium",
        "orca": "orca",
        "orcawhirlpool": "orca",
        "whirlpool": "orca",
        "meteora": "meteora",
        "phoenix": "phoenix",
        "lifinity": "lifinity",
        "jupiter": "jupiter",  # rara vez aparece como dexId, pero por si acaso
    }

    # Exact match
    if s_clean in family_map:
        return family_map[s_clean]

    # Inclusión (por si vienen sufijos/prefijos)
    for k, v in family_map.items():
        if k in s_clean:
            return v

    # Fallback: devolver sin espacios tal cual
    return s_clean

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

    pair_created_at = _parse_created_any(raw_pair.get("pairCreatedAt"))

    price_usd, price_native = _extract_price_fields(raw_pair)
    liq_usd   = _extract_liquidity_usd(raw_pair, price_usd)
    vol_usd   = _extract_volume_24h(raw_pair)
    mcap_usd  = _extract_market_cap(raw_pair)

    # Optional malformed provider nodes stay missing, without killing identity.
    txns = raw_pair.get("txns") if isinstance(raw_pair.get("txns"), dict) else {}
    txns_m5 = txns.get("m5") if isinstance(txns.get("m5"), dict) else {}
    buys_5m = _safe_float(txns_m5.get("buys"))
    sells_5m = _safe_float(txns_m5.get("sells"))
    total_5m = buys_5m + sells_5m if buys_5m is not None and sells_5m is not None else None
    price_change = raw_pair.get("priceChange") if isinstance(raw_pair.get("priceChange"), dict) else {}
    volume_change = raw_pair.get("volumeChange") if isinstance(raw_pair.get("volumeChange"), dict) else {}

    tok = {
        **raw_pair,  # Normalized identity and numbers must win over raw aliases.
        "address":        (str(mint).strip() if mint else None),  # ← MINT SPL ¡clave!
        "pair_address":   pair_address,
        "symbol":         (base.get("symbol") or raw_pair.get("symbol")),
        "price_usd":      price_usd if price_usd is not None else np.nan,
        "price_native":   price_native if price_native is not None else np.nan,
        "liquidity_usd":  liq_usd   if liq_usd   is not None else np.nan,
        "volume_24h_usd": vol_usd   if vol_usd   is not None else np.nan,
        "market_cap_usd": mcap_usd  if mcap_usd  is not None else np.nan,
        # señales rápidas
        "txns_last_5m":        total_5m,
        "txns_last_5m_buys":   buys_5m,
        "txns_last_5m_sells":  sells_5m,
        "price_pct_1m": _safe_float(price_change.get("m1")),
        "price_pct_5m": _safe_float(price_change.get("m5")),
        "volume_pct_5m": _safe_float(volume_change.get("m5")),
        "holders": _safe_float(raw_pair.get("holders")),
        # Pasar algunos campos originales por compat/debug
    }

    # ↪ Normalizar dexId y dejarlo **siempre** en `tok["dexId"]`
    # (lo hacemos *después* del merge con raw_pair para que prevalezca)
    dex_node = raw_pair.get("dex") if isinstance(raw_pair.get("dex"), dict) else {}
    dex_raw = raw_pair.get("dexId") or dex_node.get("id")
    tok["dexId"] = _normalize_dex_id(dex_raw)
    direct_liq = any(_safe_float(value) is not None for value in (
        (raw_pair.get("liquidity") or {}).get("usd") if isinstance(raw_pair.get("liquidity"), dict) else None,
        *(raw_pair.get(key) for key in ("liquidityUsd", "liquidity_locked_usd", "liquidityLockedUsd", "liqLockedUsd")),
    ))
    tok["liquidity_usd_is_proxy"] = False if direct_liq else (True if liq_usd is not None else None)
    tok["liquidity_is_proxy"] = tok["liquidity_usd_is_proxy"]

    tok = venue_clock_snapshot(tok, created_at=pair_created_at, kind="pair", source="dexscreener")
    normalized = {key: deepcopy(tok.get(key)) for key in MARKET_FIELDS}
    tok = sanitize_token_data(tok)
    tok.update(normalized)  # Raw alias coercion must not overwrite canonical observations.
    tok = _add_legacy_aliases(tok)
    return tok

def _stamp_pair_observation(pair: dict) -> dict:
    """Metadata and market values retain the same original HTTP receipt."""
    received_at = time.time()
    token = stamp_market_observation(_norm_from_pair(pair), "dexscreener", received_at=received_at)
    # Never accept a receipt supplied inside provider JSON.
    token["social_signal"] = social_signal_from_profile(pair, address=token.get("address"),
        received_at=received_at, source="dexscreener").to_dict()
    return token


# ───────────────────────── API pública ────────────────────────────
async def get_pair(address: str, *, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
    if not isinstance(address, str) or not (address := normalize_mint(address)):
        return None
    ck = f"dex:{address}"
    if force_refresh:
        cache_delete(ck)
    hit = None if force_refresh else cache_get(ck)
    if hit is not None:
        return None if hit is _SENTINEL_NIL else deepcopy(hit)

    async with aiohttp.ClientSession() as s:
        queries = (
            (_u("token-pairs/v1/solana", address), False, None),
            (_u("latest/dex/pairs/solana", address), True, None),
            (_u("latest/dex/search"), True, {"q": address}),
        )
        for url, allow_pair_address, params in queries:
            payload = await _fetch_json(url, s, params=params)
            pair = _pick_best_pair(_matching_pairs(_pair_rows(payload), address,
                allow_pair_address=allow_pair_address))
            if pair:
                res = _stamp_pair_observation(pair)
                if res.get("address"):
                    log.debug("[DEX] %s valid Solana Pair", address[:6])
                    cache_set(ck, deepcopy(res), ttl=_CACHE_TTL_OK)
                    _fail_count.pop(address, None)
                    return res

    # si llega aquí, no hubo datos
    fails = increment_failure(_fail_count, address)
    ttl = _TTL_NIL_MAX if fails >= 4 else _TTL_NIL_SHORT

    cache_set(ck, _SENTINEL_NIL, ttl=ttl)
    log.debug("[DEX] %s ❌ sin datos (TTL=%ss, capped_backoff_level=%d)", address[:6], ttl, fails)
    return None
