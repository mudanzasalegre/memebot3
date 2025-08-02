# utils/price_service.py
"""
Capa de obtención de precio/liquidez con fallback controlado.

Reglas
------
1. DexScreener es siempre la fuente primaria.
2. Si ``use_gt`` es *True* **y** DexScreener llega sin ``price_usd`` o
   ``liquidity_usd``, se consulta (una sola vez) a GeckoTerminal.
3. Se devuelven los campos combinados en un único ``dict`` o ``None`` si
   ambas fuentes fallan.
4. Se aplica un TTL-cache in-memory para no repetir llamadas iguales dentro
   del mismo ciclo.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from utils.simple_cache import cache_get, cache_set
from utils.fallback import fill_missing_fields

from fetcher import dexscreener
from fetcher.geckoterminal import get_token_data as get_gt_data, USE_GECKO_TERMINAL

# ──────────────────────────────────────────────
_TTL_OK   = 30      # s (respuesta válida)
_TTL_ERR  = 15      # s (error → reintento rápido)
_CHAIN    = "solana"
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
    """Determina si faltan campos críticos tras DexScreener."""
    if not tok:
        return True
    return any(_is_missing(tok.get(k)) for k in ("price_usd", "liquidity_usd"))


# ───────────────────────── API principal ─────────────────────────────
async def get_price(address: str, *, use_gt: bool = False) -> Optional[Dict[str, Any]]:
    """
    Devuelve métricas de precio/liquidez del *mint* ``address``.

    Parameters
    ----------
    address : str
        Mint address del token (Solana).
    use_gt : bool, default ``False``
        Solo si es ``True`` se permite consultar GeckoTerminal cuando
        DexScreener no ha devuelto datos suficientes (pares re-encolados).

    Returns
    -------
    dict | None
        Estructura normalizada o ``None`` si no se consiguió nada útil.
    """
    ck = f"price:{address}:{int(use_gt)}"
    if (hit := cache_get(ck)) is not None:              # cache hit
        return None if hit is False else hit            # False ⇒ último intento fallido

    # ① —— DexScreener (fuente primaria) ————————————————————————
    tok = await dexscreener.get_pair(address)
    if tok and not _needs_fallback(tok):
        cache_set(ck, tok, ttl=_TTL_OK)
        return tok

    # ② —— GeckoTerminal (solo si está permitido) ————————————————
    if use_gt and USE_GECKO_TERMINAL:
        gt = get_gt_data(_CHAIN, address)               # llamada síncrona con rate-limit
        if gt:
            tok = (
                fill_missing_fields(tok or {}, gt, _MISSING_FIELDS, treat_zero_as_missing=True)
                if tok
                else gt
            )
            if not _needs_fallback(tok):
                cache_set(ck, tok, ttl=_TTL_OK)
                return tok

    # ③ —— Falló todo ————————————————————————————————————————
    cache_set(ck, False, ttl=_TTL_ERR)                  # marca “sin datos” (reintento rápido)
    return None


__all__ = ["get_price"]
