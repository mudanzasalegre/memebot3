# memebot3/utils/price_service.py
"""
Capa de obtención de precio/liquidez con *fallback* controlado y conversión a USD.

Reglas
------
1. **DexScreener** → fuente primaria.
2. Si ``use_gt`` es *True* **y** DexScreener llega sin ``price_usd`` ó
   ``liquidity_usd``  ⇒ consulta **GeckoTerminal**.
3. Si tras GT sigue faltando ``price_usd`` pero existe ``price_native``,
   se convierte a USD usando el precio actual de SOL.
4. **Birdeye** (opcional, si está activado en .env) rellena los huecos
   que queden de ``_MISSING_FIELDS``.
5. TTL-cache para evitar peticiones redundantes.
"""

from __future__ import annotations

import math
import os
import logging
from typing import Any, Dict, Optional

from utils.simple_cache import cache_get, cache_set
from utils.fallback import fill_missing_fields          # ← helper común
from utils.sol_price import get_sol_usd

from fetcher import dexscreener
from fetcher.geckoterminal import get_token_data as get_gt_data, USE_GECKO_TERMINAL
from fetcher import birdeye                             # ★ NEW

logger = logging.getLogger("price_service")

# ──────────────────────────────────────────────
_TTL_OK   = 30   # s respuestas válidas
_TTL_ERR  = 15   # s error temporal
_CHAIN    = "solana"

_MISSING_FIELDS = [
    "price_usd",
    "liquidity_usd",
    "market_cap_usd",
    "volume_24h_usd",
]

_USE_BIRDEYE = os.getenv("USE_BIRDEYE", "true").lower() == "true"  # ★ NEW
# ──────────────────────────────────────────────


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


async def _price_native_to_usd(tok: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Convierte ``price_native``→``price_usd`` si procede."""
    if not tok or not _is_missing(tok.get("price_usd")):
        return tok

    price_native = tok.get("price_native")
    if _is_missing(price_native):
        return tok

    sol_usd = await get_sol_usd()
    if sol_usd:
        tok["price_usd"] = float(price_native) * sol_usd
        logger.debug(
            "[price_service] price_native %.6g × SOL_USD %.3f → price_usd %.6g",
            price_native,
            sol_usd,
            tok["price_usd"],
        )
    return tok


# ───────────────────────── API principal ──────────────────────────
async def get_price(address: str, *, use_gt: bool = False) -> Optional[Dict[str, Any]]:
    """
    Devuelve un dict con métricas de precio/liquidez o ``None``.

    Parameters
    ----------
    address : str
        Mint address del token (Solana).
    use_gt : bool, default ``False``
        Permite llamar a GeckoTerminal como segundo fallback.
    """
    ck = f"price:{address}:{int(use_gt)}"
    if (hit := cache_get(ck)) is not None:            # cache hit
        return None if hit is False else hit          # False ⇒ fallo previo

    # ① DexScreener ────────────────────────────────────────────────
    tok = await dexscreener.get_pair(address)
    if tok and not _needs_fallback(tok):
        cache_set(ck, tok, ttl=_TTL_OK)
        return tok

    # ② GeckoTerminal (opcional) ──────────────────────────────────
    if use_gt and USE_GECKO_TERMINAL:
        gt = get_gt_data(_CHAIN, address)             # sync + rate-limit
        if gt:
            tok = fill_missing_fields(tok or {}, gt, _MISSING_FIELDS, treat_zero_as_missing=True)
            if not _needs_fallback(tok):
                cache_set(ck, tok, ttl=_TTL_OK)
                logger.debug("[price_service] GT rellenó campos para %s", address[:4])
                return tok

    # ③ Conversión price_native→USD ───────────────────────────────
    tok = await _price_native_to_usd(tok)
    if tok and not _is_missing(tok.get("price_usd")):
        cache_set(ck, tok, ttl=_TTL_OK)
        return tok

    # ④ Birdeye como último recurso ───────────────────────────────
    if _USE_BIRDEYE:
        try:
            be = await birdeye.get_token_info(address)           # ★ NEW
            if not be:                      # token aún no indexado → prueba pool
                be = await birdeye.get_pool_info(address)
        except Exception as exc:                                 # ★ NEW
            logger.debug("[price_service] Birdeye error: %s", exc)
            be = None
        if be:
            tok = fill_missing_fields(tok or {}, be, _MISSING_FIELDS, treat_zero_as_missing=True)
            if not _needs_fallback(tok):
                cache_set(ck, tok, ttl=_TTL_OK)
                logger.debug("[price_service] Birdeye rellenó campos para %s", address[:4])
                return tok

    # ⑤ Sin datos válidos ─────────────────────────────────────────
    cache_set(ck, False, ttl=_TTL_ERR)           # evitan spam de llamadas
    logger.debug("[price_service] Sin datos para %s (fallback agotado)", address[:4])
    return None


# ─────────────────── Helper simplificado ──────────────────────
async def get_price_usd(address: str, *, use_gt: bool = True) -> float | None:
    """
    Devuelve sólo ``price_usd`` (float) o ``None``.
    Por defecto permite GT, y por extensión Birdeye, en el fallback.
    """
    tok = await get_price(address, use_gt=use_gt)
    return float(tok["price_usd"]) if tok and not _is_missing(tok.get("price_usd")) else None


__all__ = ["get_price", "get_price_usd"]
