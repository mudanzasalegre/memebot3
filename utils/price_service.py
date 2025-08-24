# memebot3/utils/price_service.py
"""
Capa de obtención de precio/liquidez con *fallback* controlado y conversión a USD.

Orden de fuentes (2025-08):
1. DexScreener → fuente primaria.
2. Birdeye (si está activado en .env) rellena huecos críticos.
3. GeckoTerminal (si use_gt=True) para huecos restantes.
4. Conversión: price_native × SOL_USD si sigue faltando price_usd.
5. (NUEVO) Jupiter Price v3 (Lite) SOLO en consultas de “solo precio”.

Extras:
• Reintento corto de toda la cadena ante fallo transitorio.
• Cacheo de aciertos y fallos (TTL configurable vía .env DEXS_TTL_NIL).
• Bloqueo de direcciones no Solana (0x…).
• Modo “solo precio”: en cierres/consultas rápidas aceptamos price_usd
  aunque falte liquidity_usd (evita caer a fallback del buy_price).
"""

from __future__ import annotations

import math
import os
import logging
from typing import Any, Dict, Optional, Tuple

from utils.simple_cache import cache_get, cache_set
from utils.fallback import fill_missing_fields
from utils.sol_price import get_sol_usd

from fetcher import dexscreener
from fetcher.geckoterminal import get_token_data as get_gt_data, USE_GECKO_TERMINAL
from fetcher import birdeye

# (NUEVO) Jupiter Price v3 (Lite) como fallback final para "solo precio"
try:
    from fetcher.jupiter_price import get_usd_price as _jup_get_usd_price  # type: ignore
except Exception:  # pragma: no cover
    _jup_get_usd_price = None  # se comprobará en runtime

logger = logging.getLogger("price_service")

# ───────────────────────── configuración / constantes ─────────────────────────
_TTL_OK   = int(os.getenv("DEXS_TTL_OK", "30"))           # s para respuestas válidas
_TTL_ERR  = int(os.getenv("DEXS_TTL_NIL", "15"))          # s para cachear fallos
_CHAIN    = "solana"

_MISSING_FIELDS = [
    "price_usd",
    "liquidity_usd",
    "market_cap_usd",
    "volume_24h_usd",
]

_USE_BIRDEYE    = os.getenv("USE_BIRDEYE", "true").lower() == "true"
_RETRY_ON_FAIL  = int(os.getenv("PRICE_RETRY_ON_FAIL", "1"))  # nº reintentos de la cadena
_RETRY_DELAY_S  = float(os.getenv("PRICE_RETRY_DELAY_S", "2.0"))

# (NUEVO) Flag de entorno para activar/desactivar Jupiter como último fallback
_USE_JUPITER_PRICE = os.getenv("USE_JUPITER_PRICE", "true").lower() == "true"

_REQUIRED_FOR_FULL  : Tuple[str, ...] = ("price_usd", "liquidity_usd")  # validación completa
_REQUIRED_FOR_PRICE : Tuple[str, ...] = ("price_usd",)                  # solo precio (cierres)


# ─────────────────────────────────── utils ────────────────────────────────────
def _f(x):
    """Convierte a float o devuelve None si no es convertible."""
    try:
        return float(x)
    except Exception:
        return None


def _coerce_tick_numbers(tick: dict | None) -> dict:
    """
    Convierte a float los campos típicos y aplana anidados si el adapter
    devolvió estructuras como {"liquidity":{"usd":...}} o strings.
    """
    if not isinstance(tick, dict):
        return {}

    t = dict(tick)

    # Precio USD (varía entre adapters)
    t["price_usd"] = _f(t.get("price_usd") or t.get("priceUsd"))

    # Liquidez USD
    liq = t.get("liquidity_usd")
    if liq is None:
        liq = (t.get("liquidity") or {}).get("usd")
    t["liquidity_usd"] = _f(liq)

    # Volumen 24h USD
    vol = t.get("volume_24h_usd")
    if vol is None:
        vol = (t.get("volume") or {}).get("h24")
    t["volume_24h_usd"] = _f(vol)

    # Market cap / FDV
    t["market_cap_usd"] = _f(t.get("market_cap_usd") or t.get("fdv") or t.get("mcap"))

    # Precio nativo: evitar dict/list
    pn = t.get("price_native")
    if isinstance(pn, (dict, list, tuple)):
        t["price_native"] = None
    else:
        t["price_native"] = _f(pn)

    return t


def _is_missing(val: Any) -> bool:
    """True si val es None, NaN o 0."""
    if val is None:
        return True
    if isinstance(val, float) and math.isnan(val):
        return True
    return val == 0


def _needs_fields(tok: Dict[str, Any] | None, fields: Tuple[str, ...]) -> bool:
    """True si faltan *cualesquiera* de los campos pedidos."""
    if not tok:
        return True
    return any(_is_missing(tok.get(k)) for k in fields)


def _is_solana_address(addr: str) -> bool:
    """
    Filtro defensivo de address:
      • Descarta EVM (0x…)
      • Acepta longitudes típicas base58 (dejamos margen 30–50).
    """
    if not addr or addr.startswith("0x"):
        return False
    return 30 <= len(addr) <= 50


async def _price_native_to_usd(tok: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Convierte ``price_native``→``price_usd`` si procede y es seguro."""
    if not tok or not _is_missing(tok.get("price_usd")):
        return tok

    price_native = tok.get("price_native")
    if _is_missing(price_native):
        return tok

    sol_usd = await get_sol_usd()
    if sol_usd:
        try:
            pn = float(price_native)
            su = float(sol_usd)
            tok["price_usd"] = pn * su
            logger.debug(
                "[price_service] price_native %.6g × SOL_USD %.3f → price_usd %.6g",
                pn, su, tok["price_usd"],
            )
        except Exception:
            # Si algo raro viene en price_native, lo anulamos para evitar dict*float
            tok["price_native"] = None
    return tok


def _normalize_after_merge(tok: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Aplica coerción tras combinar fuentes (post fill_missing_fields)."""
    if tok is None:
        return None
    return _coerce_tick_numbers(tok)


# ───────────────────── pipeline de fuentes (sin caché) ───────────────────────
async def _query_sources(address: str, *, use_gt: bool, fields_needed: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
    """
    Ejecuta la cadena de fuentes y devuelve `tok` con los campos pedidos
    en 'fields_needed' completados en la medida de lo posible. No cachea.
    """
    tok: Dict[str, Any] | None = None

    # ① DexScreener
    try:
        ds = await dexscreener.get_pair(address)
        tok = _coerce_tick_numbers(ds)
    except Exception as exc:
        logger.debug("[price_service] DexScreener error: %s", exc)
        tok = None

    if tok and not _needs_fields(tok, fields_needed):
        logger.debug("[price_service] DexScreener OK para %s…", address[:6])
        return tok

    # ② Birdeye (opcional)
    if _USE_BIRDEYE:
        be: Dict[str, Any] | None = None
        try:
            be = await birdeye.get_token_info(address)
            if not be:
                be = await birdeye.get_pool_info(address)
            be = _coerce_tick_numbers(be)
        except Exception as exc:
            logger.debug("[price_service] Birdeye error: %s", exc)
            be = None

        if be:
            logger.debug("[price_service] Fallback → Birdeye para %s…", address[:6])
            merged = fill_missing_fields(tok or {}, be, _MISSING_FIELDS, treat_zero_as_missing=True)
            tok = _normalize_after_merge(merged)
            if tok and not _needs_fields(tok, fields_needed):
                return tok

    # ③ GeckoTerminal (opcional)
    if use_gt and USE_GECKO_TERMINAL:
        try:
            gt = get_gt_data(_CHAIN, address)
            gt = _coerce_tick_numbers(gt)
        except Exception as exc:
            logger.debug("[price_service] GeckoTerminal error: %s", exc)
            gt = None

        if gt:
            logger.debug("[price_service] Fallback → GeckoTerminal para %s…", address[:6])
            merged = fill_missing_fields(tok or {}, gt, _MISSING_FIELDS, treat_zero_as_missing=True)
            tok = _normalize_after_merge(merged)
            if tok and not _needs_fields(tok, fields_needed):
                return tok

    # ④ Conversión price_native→USD (segura)
    tok = _normalize_after_merge(await _price_native_to_usd(tok))
    if tok and not _needs_fields(tok, fields_needed):
        logger.debug("[price_service] Fallback → native×SOL para %s…", address[:6])
        return tok

    # ⑤ Jupiter Price v3 (Lite) SOLO si pedimos "solo precio"
    if (
        _USE_JUPITER_PRICE
        and _jup_get_usd_price is not None
        and tuple(fields_needed) == _REQUIRED_FOR_PRICE
        and _needs_fields(tok, fields_needed)
    ):
        try:
            jup_price = await _jup_get_usd_price(address)
        except Exception as exc:
            logger.debug("[price_service] Jupiter error: %s", exc)
            jup_price = None

        if jup_price and not _is_missing(jup_price):
            if not tok:
                tok = {}
            try:
                tok["price_usd"] = float(jup_price)
            except Exception:
                tok["price_usd"] = jup_price  # por si viene ya como float/Decimal
            tok = _coerce_tick_numbers(tok)
            logger.debug("[price_service] Fallback → Jupiter (price_only) para %s…", address[:6])
            return tok

    # ⑥ Sin datos suficientes para los campos solicitados
    return tok  # puede ser dict incompleto o None


# ───────────────────────── API principal ──────────────────────────
async def get_price(
    address: str,
    *,
    use_gt: bool = False,
    critical: bool = False,
    price_only: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Devuelve un dict con métricas de precio/liquidez o ``None``.

    Params
    ------
    address : str
    use_gt : bool
        Permite llamar a GeckoTerminal como tercer fallback.
    critical : bool
        Si True, ignora cache negativa y no la escribe (modo cierre).
    price_only : bool
        Si True, exige SOLO `price_usd` (cierres/compras rápidas).
        Si False, exige `price_usd` + `liquidity_usd` (validaciones).
    """
    if not _is_solana_address(address):
        # cache negativo corto para no martillear (salvo en crítico)
        if not critical:
            cache_set(f"price:{address}:bad", False, ttl=_TTL_ERR)
        logger.debug("[price_service] Address no-Solana bloqueada: %r", address)
        return None

    fields_needed = _REQUIRED_FOR_PRICE if price_only else _REQUIRED_FOR_FULL
    ck = f"price:{address}:{int(use_gt)}:{int(price_only)}"

    hit = cache_get(ck)
    if hit is not None:
        if hit is False:
            if critical:
                logger.debug("[price_service] critical=True: ignorando cache negativa para %s", address[:6])
            else:
                return None  # respetamos caché negativa en modo normal
        else:
            # reforzamos tipos por si vino de disco
            return _coerce_tick_numbers(hit)

    # Primer intento de la cadena
    tok = await _query_sources(address, use_gt=use_gt, fields_needed=fields_needed)
    if tok and not _needs_fields(tok, fields_needed):
        cache_set(ck, tok, ttl=_TTL_OK)
        return tok

    # Reintento corto (fallos transitorios)
    if _RETRY_ON_FAIL > 0:
        try:
            import asyncio
            await asyncio.sleep(_RETRY_DELAY_S)
        except Exception:
            pass
        tok_retry = await _query_sources(address, use_gt=use_gt, fields_needed=fields_needed)
        if tok_retry and not _needs_fields(tok_retry, fields_needed):
            cache_set(ck, tok_retry, ttl=_TTL_OK)
            return tok_retry
        tok = tok_retry or tok

    if tok and not _needs_fields(tok, fields_needed):
        cache_set(ck, tok, ttl=_TTL_OK)
        return tok

    # Sin datos válidos → sólo cache negativa si NO es crítico
    if not critical:
        cache_set(ck, False, ttl=_TTL_ERR)
    logger.debug(
        "[price_service] Sin datos (%s) para %s (fallback agotado; critical=%s)",
        "price_only" if price_only else "full",
        address[:6],
        critical,
    )
    return None


# ─────────────────── Helper simplificado ──────────────────────
async def get_price_usd(address: str, *, use_gt: bool = True, critical: bool = False) -> float | None:
    """
    Devuelve sólo ``price_usd`` (float) o ``None``.
    En cierres/compras rápidas no exigimos liquidez (price_only=True).
    En crítico ignoramos caché negativa y no la escribimos.
    """
    tok = await get_price(address, use_gt=use_gt, critical=critical, price_only=True)
    return float(tok["price_usd"]) if tok and not _is_missing(tok.get("price_usd")) else None


__all__ = ["get_price", "get_price_usd"]
