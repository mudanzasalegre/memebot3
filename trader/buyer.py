"""
trader.buyer
-------------
Capa delgada sobre ``gmgn.buy`` que añade comprobaciones de saldo
y reserva de gas antes de lanzar la orden real. 100 % compatible
con el flujo original de MemeBot 3 (la firma de retorno NO cambia).

• Cuando *amount_sol* ≤ 0  →  modo simulación (paper‑trading).
• Antes de comprar, verifica que el wallet dispone de saldo suficiente
  para cubrir la orden **y** deja un `GAS_RESERVE_SOL` para las ventas.
• Devuelve SIEMPRE un dict homogéneo:

    {
      "qty_lamports": int,     # cantidad comprada (lamports)
      "signature":    str,     # txid o flag especial
      "route":        dict     # JSON crudo de gmgn
    }

Depende de:
    · utils.solana_rpc.get_balance_lamports()
    · config.config.CFG (SOL_PUBLIC_KEY y LOG_LEVEL)
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, Final

from config.config import CFG
from utils.solana_rpc import get_balance_lamports

# gmgn SDK local
from . import gmgn  # type: ignore

log = logging.getLogger("buyer")

# ——— Parámetros ——————————————————————————————————————
GAS_RESERVE_SOL: Final[float] = 0.002        # ≈ 2 mSOL p/fees de salida
_GAS_RESERVE_LAMPORTS: Final[int] = int(GAS_RESERVE_SOL * 1e9)

_RETRIES: Final[int] = 3
_RETRY_WAIT: Final[int] = 2          # s entre intentos

_WALLET_PUBKEY: Final[str] = os.getenv("SOL_PUBLIC_KEY", "")

# ——— helpers internos ——————————————————————————————
def _parse_route(resp: dict) -> dict:
    """Normaliza la respuesta de gmgn a un dict uniforme."""
    route = resp.get("route", {})
    quote = route.get("quote", {})
    qty_lamports = int(quote.get("outAmount", "0"))
    return {
        "qty_lamports": qty_lamports,
        "signature": resp.get("signature", ""),
        "route": route,
    }

async def _has_enough_funds(amount_sol: float) -> bool:
    """Comprueba que queda SOL suficiente + reserva para gas."""
    if not _WALLET_PUBKEY:
        # Sin clave pública -> asumimos que sí hay fondos
        return True
    try:
        balance_lp = await get_balance_lamports(_WALLET_PUBKEY)
        need_lp = int(amount_sol * 1e9) + _GAS_RESERVE_LAMPORTS
        return balance_lp >= need_lp
    except Exception as exc:                   # noqa: BLE001
        log.warning("[buyer] balance check error: %s", exc)
        # Si el RPC falla, conservador: permitimos la orden
        return True

# ——— API pública ——————————————————————————————————————
async def buy(token_addr: str, amount_sol: float) -> Dict[str, object]:
    """
    Compra real o simulada.

    Parameters
    ----------
    token_addr : str
        Token mint address (Solana).
    amount_sol : float
        Size en SOL. Si es ≤ 0 → simulación (paper).
    """

    # —— Modo simulación / paper ——————————————
    if amount_sol <= 0:
        log.info("[buyer] SIMULACIÓN · no se envía orden real (amount=0)")
        return {
            "qty_lamports": 0,
            "signature": "SIMULATION",
            "route": {},
        }

    # —— Comprobación saldo / gas ——————————————
    if not await _has_enough_funds(amount_sol):
        log.error(
            "[buyer] Fondos insuficientes · %.3f SOL pedido · reserva %.3f SOL",
            amount_sol, GAS_RESERVE_SOL
        )
        return {
            "qty_lamports": 0,
            "signature": "INSUFFICIENT_FUNDS",
            "route": {},
        }

    # —— Llamada a gmgn con reintentos ——————————
    last_exc: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = await gmgn.buy(token_addr, amount_sol)
            return _parse_route(resp)
        except Exception as exc:               # noqa: BLE001
            last_exc = exc
            log.warning("[buyer] gmgn.buy fallo (%s/%s): %s",
                       attempt, _RETRIES, exc)
            if attempt < _RETRIES:
                await asyncio.sleep(_RETRY_WAIT)

    # —— Si todas fallan ——————————————
    log.error("[buyer] gmgn.buy agotó reintentos: %s", last_exc)
    return {
        "qty_lamports": 0,
        "signature": "BUY_FAILED",
        "route": {},
    }
