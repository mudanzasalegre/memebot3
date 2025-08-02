"""utils/sol_price.py
Obtiene el precio spot de **1 SOL en USD** usando la API pública de CoinGecko.

Características
---------------
- Llamada **asíncrona** vía `aiohttp` (timeout configurable).
- Caché in‑memory con TTL configurable (por defecto 60 s).
- Manejo de errores: devuelve ``None`` si la API falla o responde con error.
- Exporte único: ``get_sol_usd() → float | None``.
"""
from __future__ import annotations

import os
import time
from typing import Optional, Tuple

import aiohttp
import logging
logger = logging.getLogger("sol_price")

# ──────────────────────────────────────────────
_COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
)
_TTL_OK = int(os.getenv("COINGECKO_SOL_TTL", 60))      # seg (precio válido)
_TIMEOUT = float(os.getenv("COINGECKO_TIMEOUT", 6))    # seg

# Caché sencilla: (precio_usd, exp_timestamp)
_CACHE: Optional[Tuple[float, float]] = None
# ──────────────────────────────────────────────


async def _fetch_sol_usd() -> Optional[float]:
    """Consulta CoinGecko y devuelve el precio (float) o ``None``."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_COINGECKO_URL, timeout=_TIMEOUT) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[sol_price] CoinGecko respondió HTTP {resp.status}"
                    )
                    return None
                data = await resp.json()
                return float(data["solana"]["usd"])
    except Exception as exc:  # noqa: BLE001 – cualquier fallo IO/JSON
        logger.warning(f"[sol_price] Error solicitando precio SOL: {exc}")
        return None


async def get_sol_usd() -> Optional[float]:
    """Devuelve el precio actual de 1 SOL en USD o ``None`` si no se obtuvo."""
    global _CACHE
    now = time.time()

    # ① — Cache hit válido ————————————————————————————
    if _CACHE and now < _CACHE[1]:
        return _CACHE[0]

    # ② — Fetch a CoinGecko ———————————————————————————
    price = await _fetch_sol_usd()
    if price is not None:
        _CACHE = (price, now + _TTL_OK)
        return price

    # ③ — Error → cache fallida corta para evitar spam ——
    _CACHE = (0.0, now + 10)  # 10 s de back‑off
    return None


__all__ = ["get_sol_usd"]
