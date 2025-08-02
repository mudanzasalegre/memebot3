"""
Capa de obtención de precio/liquidez con *fallback* controlado y conversión a USD.

Reglas
------
1. **DexScreener** es siempre la fuente primaria.
2. Si ``use_gt`` es *True* **y** DexScreener llega sin ``price_usd`` o
   ``liquidity_usd``, se consulta (una sola vez) a **GeckoTerminal**.
3. Si todavía falta ``price_usd`` pero existe ``price_native`` (precio en SOL),
   se multiplica por el precio actual del SOL para estimar el valor en USD.
4. (Opcional) Aquí podría añadirse un 3er *fallback* (Jupiter u otra API).
5. Se devuelven los campos combinados en un único ``dict`` o ``None`` si
   todas las fuentes fallan.
6. Se aplica un TTL‑cache en memoria para no repetir llamadas idénticas dentro
   del mismo ciclo.

El módulo también expone ``get_price_usd(address)`` que devuelve directamente
el *float* con el mejor ``price_usd`` disponible.
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, Optional

import logging
logger = logging.getLogger("price_service")

from utils.simple_cache import cache_get, cache_set
from utils.fallback import fill_missing_fields
from utils.sol_price import get_sol_usd

from fetcher import dexscreener
from fetcher.geckoterminal import (
    get_token_data as get_gt_data,
    USE_GECKO_TERMINAL,
)

# ──────────────────────────────────────────────
_TTL_OK = 30     # s (respuesta válida)
_TTL_ERR = 15    # s (error → reintento rápido)
_CHAIN = "solana"
_MISSING_FIELDS = [
    "price_usd",
    "liquidity_usd",
    "market_cap_usd",
    "volume_24h_usd",
]
# ──────────────────────────────────────────────


def _is_missing(val: Any) -> bool:
    """True si val es None, NaN o 0."""
    if val is None:
        return True
    if isinstance(val, float) and math.isnan(val):
        return True
    return val == 0


def _needs_fallback(tok: Dict[str, Any] | None) -> bool:
    """Determina si faltan campos críticos tras DexScreener/GT."""
    if not tok:
        return True
    return any(_is_missing(tok.get(k)) for k in ("price_usd", "liquidity_usd"))


async def _price_native_to_usd(tok: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Convierte ``price_native``→``price_usd`` si fuera necesario."""
    if not tok:
        return None
    if not _is_missing(tok.get("price_usd")):
        return tok  # ya tiene price_usd válido
    price_native = tok.get("price_native")
    if _is_missing(price_native):
        return tok  # no hay forma de calcular

    sol_usd = await get_sol_usd()
    if sol_usd:
        tok["price_usd"] = float(price_native) * sol_usd
        logger.debug(
            f"[price_service] Conversión price_native→USD: {tok['price_usd']:.6f} USD"
        )
    return tok


# ───────────────────────── API principal ─────────────────────────────
async def get_price(address: str, *, use_gt: bool = False) -> Optional[Dict[str, Any]]:
    """Devuelve un ``dict`` con métricas de precio/liquidez o ``None``.

    Parameters
    ----------
    address : str
        Mint address del token (Solana).
    use_gt : bool, default ``False``
        Si es ``True`` se permite consultar GeckoTerminal cuando DexScreener no
        ha devuelto datos suficientes.
    """
    ck = f"price:{address}:{int(use_gt)}"
    if (hit := cache_get(ck)) is not None:  # cache hit
        return None if hit is False else hit  # False ⇒ último intento fallido

    # ① —— DexScreener (fuente primaria) ————————————————————————
    tok = await dexscreener.get_pair(address)
    if tok and not _needs_fallback(tok):
        cache_set(ck, tok, ttl=_TTL_OK)
        return tok

    # ② —— GeckoTerminal (solo si está permitido) ————————————————
    if use_gt and USE_GECKO_TERMINAL:
        gt = get_gt_data(_CHAIN, address)  # llamada síncrona con rate‑limit
        if gt:
            tok = (
                fill_missing_fields(tok or {}, gt, _MISSING_FIELDS, treat_zero_as_missing=True)
                if tok
                else gt
            )
            if not _needs_fallback(tok):
                cache_set(ck, tok, ttl=_TTL_OK)
                return tok

    # ③ —— Intentar conversión price_native→USD ————————————————
    if tok:
        tok = await _price_native_to_usd(tok)
        if tok and not _is_missing(tok.get("price_usd")):
            cache_set(ck, tok, ttl=_TTL_OK)
            return tok

    # ④ —— Falló todo ————————————————————————————————————————
    cache_set(ck, False, ttl=_TTL_ERR)  # marca “sin datos” (reintento rápido)
    return None


# ────────────────── Helper: sólo el número en USD ───────────────────
async def get_price_usd(address: str, *, use_gt: bool = True) -> float | None:
    """Shortcut que devuelve únicamente ``price_usd`` como *float* (o ``None``)."""
    tok = await get_price(address, use_gt=use_gt)
    return float(tok["price_usd"]) if tok and not _is_missing(tok.get("price_usd")) else None


__all__ = [
    "get_price",
    "get_price_usd",
]
