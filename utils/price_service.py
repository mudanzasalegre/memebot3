# memebot3/utils/price_service.py
"""
Capa de obtención de precio/liquidez con *fallback* controlado y conversión a USD.

Orden de fuentes (2025-08):
1. DexScreener → fuente primaria.
2. Birdeye (si está activado en .env) rellena huecos críticos.
3. GeckoTerminal (si use_gt=True) para huecos restantes.
4. Conversión: price_native × SOL_USD si sigue faltando price_usd.

Extras:
• Reintento corto de toda la cadena ante fallo transitorio.
• Cacheo de aciertos y fallos (TTL configurable vía .env DEXS_TTL_NIL).
• Bloqueo de direcciones no Solana (0x…).
"""

from __future__ import annotations

import math
import os
import logging
from typing import Any, Dict, Optional

from utils.simple_cache import cache_get, cache_set
from utils.fallback import fill_missing_fields
from utils.sol_price import get_sol_usd

from fetcher import dexscreener
from fetcher.geckoterminal import get_token_data as get_gt_data, USE_GECKO_TERMINAL
from fetcher import birdeye

logger = logging.getLogger("price_service")

# ───────────────────────── configuración / constantes ─────────────────────────
_TTL_OK   = int(os.getenv("DEXS_TTL_OK", 30))            # s para respuestas válidas
_TTL_ERR  = int(os.getenv("DEXS_TTL_NIL", "15"))         # s para cachear fallos
_CHAIN    = "solana"

_MISSING_FIELDS = [
    "price_usd",
    "liquidity_usd",
    "market_cap_usd",
    "volume_24h_usd",
]

_USE_BIRDEYE = os.getenv("USE_BIRDEYE", "true").lower() == "true"
_RETRY_ON_FAIL = int(os.getenv("PRICE_RETRY_ON_FAIL", "1"))  # nº reintentos de la cadena
_RETRY_DELAY_S = float(os.getenv("PRICE_RETRY_DELAY_S", "2.0"))

# ─────────────────────────────────── utils ────────────────────────────────────
def _is_missing(val: Any) -> bool:
    """True si val es None, NaN o 0."""
    if val is None:
        return True
    if isinstance(val, float) and math.isnan(val):
        return True
    return val == 0


def _needs_fallback(tok: Dict[str, Any] | None) -> bool:
    """¿Faltan datos críticos tras la última fuente?"""
    if not tok:
        return True
    return any(_is_missing(tok.get(k)) for k in ("price_usd", "liquidity_usd"))


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
    """Convierte ``price_native``→``price_usd`` si procede."""
    if not tok or not _is_missing(tok.get("price_usd")):
        return tok

    price_native = tok.get("price_native")
    if _is_missing(price_native):
        return tok

    sol_usd = await get_sol_usd()
    if sol_usd:
        try:
            tok["price_usd"] = float(price_native) * float(sol_usd)
            logger.debug(
                "[price_service] price_native %.6g × SOL_USD %.3f → price_usd %.6g",
                price_native,
                sol_usd,
                tok["price_usd"],
            )
        except Exception:
            pass
    return tok


async def _query_sources(address: str, *, use_gt: bool) -> Optional[Dict[str, Any]]:
    """
    Ejecuta la cadena de fuentes y devuelve `tok` con los campos críticos
    completados en la medida de lo posible. No cachea (eso se hace arriba).
    """
    tok: Dict[str, Any] | None = None

    # ① DexScreener
    try:
        tok = await dexscreener.get_pair(address)
    except Exception as exc:
        logger.debug("[price_service] DexScreener error: %s", exc)
        tok = None

    if tok and not _needs_fallback(tok):
        logger.debug("[price_service] DexScreener OK para %s…", address[:6])
        return tok

    # ② Birdeye (opcional)
    if _USE_BIRDEYE:
        be: Dict[str, Any] | None = None
        try:
            be = await birdeye.get_token_info(address)
            if not be:
                be = await birdeye.get_pool_info(address)
        except Exception as exc:
            logger.debug("[price_service] Birdeye error: %s", exc)
            be = None
        if be:
            logger.debug("[price_service] Fallback → Birdeye para %s…", address[:6])
            tok = fill_missing_fields(tok or {}, be, _MISSING_FIELDS, treat_zero_as_missing=True)
            if not _needs_fallback(tok):
                return tok

    # ③ GeckoTerminal (opcional)
    if use_gt and USE_GECKO_TERMINAL:
        try:
            gt = get_gt_data(_CHAIN, address)
        except Exception as exc:
            logger.debug("[price_service] GeckoTerminal error: %s", exc)
            gt = None
        if gt:
            logger.debug("[price_service] Fallback → GeckoTerminal para %s…", address[:6])
            tok = fill_missing_fields(tok or {}, gt, _MISSING_FIELDS, treat_zero_as_missing=True)
            if not _needs_fallback(tok):
                return tok

    # ④ Conversión price_native→USD
    tok = await _price_native_to_usd(tok)
    if tok and not _is_missing(tok.get("price_usd")):
        logger.debug("[price_service] Fallback → native×SOL para %s…", address[:6])
        return tok

    # ⑤ Sin datos
    return tok  # puede ser dict incompleto o None


# ───────────────────────── API principal ──────────────────────────
async def get_price(address: str, *, use_gt: bool = False) -> Optional[Dict[str, Any]]:
    """
    Devuelve un dict con métricas de precio/liquidez o ``None``.

    Parameters
    ----------
    address : str
        Mint address del token (Solana).
    use_gt : bool, default ``False``
        Permite llamar a GeckoTerminal como tercer fallback.
    """
    if not _is_solana_address(address):
        # cache negativo corto para no martillear
        ck_bad = f"price:{address}:bad"
        cache_set(ck_bad, False, ttl=_TTL_ERR)
        logger.debug("[price_service] Address no-Solana bloqueada: %r", address)
        return None

    ck = f"price:{address}:{int(use_gt)}"
    hit = cache_get(ck)
    if hit is not None:
        return None if hit is False else hit  # False ⇒ fallo previo

    # Primer intento de la cadena
    tok = await _query_sources(address, use_gt=use_gt)
    if tok and not _needs_fallback(tok):
        cache_set(ck, tok, ttl=_TTL_OK)
        return tok

    # Reintento corto de toda la cadena (fallos transitorios de APIs)
    if _RETRY_ON_FAIL > 0:
        try:
            import asyncio
            await asyncio.sleep(_RETRY_DELAY_S)
        except Exception:
            pass
        tok_retry = await _query_sources(address, use_gt=use_gt)
        if tok_retry and not _needs_fallback(tok_retry):
            cache_set(ck, tok_retry, ttl=_TTL_OK)
            return tok_retry
        # preferimos el mejor de los dos (si el primero tenía algo útil)
        tok = tok_retry or tok

    # Conversión final ya se intentó en _query_sources; si seguimos cojos:
    if tok and not _needs_fallback(tok):
        cache_set(ck, tok, ttl=_TTL_OK)
        return tok

    # Sin datos válidos
    cache_set(ck, False, ttl=_TTL_ERR)
    logger.debug("[price_service] Sin datos para %s (fallback agotado)", address[:6])
    return None


# ─────────────────── Helper simplificado ──────────────────────
async def get_price_usd(address: str, *, use_gt: bool = True) -> float | None:
    """
    Devuelve sólo ``price_usd`` (float) o ``None``.
    Por defecto permite GT (y Birdeye antes), en el fallback.
    """
    tok = await get_price(address, use_gt=use_gt)
    return float(tok["price_usd"]) if tok and not _is_missing(tok.get("price_usd")) else None


__all__ = ["get_price", "get_price_usd"]
