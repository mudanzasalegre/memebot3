# trader/buyer.py
"""
Capa delgada sobre ``gmgn.buy`` que añade comprobaciones de saldo
y reserva de gas antes de lanzar la orden real. 100 % compatible
con el flujo original de MemeBot 3 (la firma de retorno NO cambia).

• Cuando *amount_sol* ≤ 0  →  modo simulación (paper-trading).
• Antes de comprar, verifica que el wallet dispone de saldo suficiente
  para cubrir la orden **y** deja un `GAS_RESERVE_SOL` para las ventas.
• Devuelve SIEMPRE un dict homogéneo:

    {
      "qty_lamports": int,     # cantidad comprada (lamports)
      "signature":    str,     # txid o flag especial
      "route":        dict     # JSON crudo de gmgn
      "buy_price_usd": float,  # precio unitario de entrada (USD)
      "peak_price":    float,  # precio máximo observado (USD)
    }
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, Final

from config.config import CFG
from utils.solana_rpc import get_balance_lamports
from utils import price_service                  # ← helper get_price_usd()
from db.database import SessionLocal
from db.models import Position
from sqlalchemy import select

# gmgn SDK local
from . import gmgn  # type: ignore

log = logging.getLogger("buyer")

# ─── Parámetros ──────────────────────────────────────────────
GAS_RESERVE_SOL: Final[float] = 0.002
_GAS_RESERVE_LAMPORTS: Final[int] = int(GAS_RESERVE_SOL * 1e9)

_RETRIES: Final[int] = 3
_RETRY_WAIT: Final[int] = 2  # s entre intentos

_WALLET_PUBKEY: Final[str] = os.getenv("SOL_PUBLIC_KEY", "")


# ─── Helpers ─────────────────────────────────────────────────
def _parse_route(resp: dict) -> tuple[int, float, dict]:
    """
    Normaliza la respuesta de gmgn:
    qty_lamports (outAmount), price_usd (unitario) y ruta bruta.
    """
    route = resp.get("route", {})
    quote = route.get("quote", {})

    qty_lp = int(quote.get("outAmount", "0"))
    # inAmountUSD es el coste TOTAL de la compra. Dividimos por las unidades compradas.
    total_usd = float(quote.get("inAmountUSD", "0.0"))
    price_unit = (total_usd / qty_lp * 1e9) if qty_lp else 0.0
    return qty_lp, price_unit, route


async def _has_enough_funds(amount_sol: float) -> bool:
    """Comprueba que queda SOL suficiente + reserva para gas."""
    if not _WALLET_PUBKEY:
        return True
    try:
        balance_lp = await get_balance_lamports(_WALLET_PUBKEY)
        needed_lp = int(amount_sol * 1e9) + _GAS_RESERVE_LAMPORTS
        return balance_lp >= needed_lp
    except Exception as exc:  # noqa: BLE001
        log.warning("[buyer] balance check error: %s", exc)
        return True


async def _max_positions_reached() -> bool:
    """Comprueba si ya hay demasiadas posiciones abiertas."""
    async with SessionLocal() as session:
        stmt = select(Position).where(Position.closed.is_(False))
        count = (await session.execute(stmt)).scalars().unique().count()
        return count >= CFG.MAX_ACTIVE_POSITIONS


async def _price_fallback(token_addr: str) -> float:
    """Obtiene price_usd mediante servicio externo si gmgn no lo devolvió."""
    try:
        price = await price_service.get_price_usd(token_addr)
        return float(price) if price else 0.0
    except Exception as exc:  # noqa: BLE001
        log.warning("[buyer] fallback price error: %s", exc)
        return 0.0


# ─── API pública ─────────────────────────────────────────────
async def buy(token_addr: str, amount_sol: float) -> Dict[str, object]:
    """
    Compra real o simulada.

    Parameters
    ----------
    token_addr : str
        Token mint address (Solana).
    amount_sol : float
        Tamaño en SOL. Si es ≤ 0 → simulación (paper).
    """
    # ─────── Simulación directa (paper-trading) ────────────
    if amount_sol <= 0:
        log.info("[buyer] SIMULACIÓN · no se envía orden real (amount=0)")
        price_usd_sim = await _price_fallback(token_addr)
        return {
            "qty_lamports": 0,
            "signature": "SIMULATION",
            "route": {},
            "buy_price_usd": price_usd_sim,
            "peak_price": price_usd_sim,
        }

    # ─────── Límite de posiciones / fondos ────────────────
    if await _max_positions_reached():
        log.warning("[buyer] Límite de posiciones abiertas alcanzado (%d)", CFG.MAX_ACTIVE_POSITIONS)
        return {
            "qty_lamports": 0,
            "signature": "LIMIT_REACHED",
            "route": {},
            "buy_price_usd": 0.0,
            "peak_price": 0.0,
        }

    if not await _has_enough_funds(amount_sol):
        log.error("[buyer] Fondos insuficientes · %.3f SOL pedido · reserva %.3f SOL",
                  amount_sol, GAS_RESERVE_SOL)
        return {
            "qty_lamports": 0,
            "signature": "INSUFFICIENT_FUNDS",
            "route": {},
            "buy_price_usd": 0.0,
            "peak_price": 0.0,
        }

    # ─────── Intentos de compra real ───────────────────────
    last_exc: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = await gmgn.buy(token_addr, amount_sol)
            qty_lp, price_unit, route = _parse_route(resp)

            # Si gmgn no devolvió precio unitario, usamos fallback externo
            if price_unit == 0.0:
                price_unit = await _price_fallback(token_addr)

            return {
                "qty_lamports": qty_lp,
                "signature": resp.get("signature", ""),
                "route": route,
                "buy_price_usd": price_unit,
                "peak_price": price_unit,
            }

        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("[buyer] gmgn.buy fallo (%s/%s): %s", attempt, _RETRIES, exc)
            if attempt < _RETRIES:
                await asyncio.sleep(_RETRY_WAIT)

    # ─────── Fracaso definitivo ────────────────────────────
    log.error("[buyer] gmgn.buy agotó reintentos: %s", last_exc)
    return {
        "qty_lamports": 0,
        "signature": "BUY_FAILED",
        "route": {},
        "buy_price_usd": 0.0,
        "peak_price": 0.0,
    }
