# trader/seller.py
"""
Wrapper de gmgn.sell con parsing homogéneo + lógica de cierre de posiciones

Devuelve:
    {
      signature : str
      route     : dict   # raw JSON de GMGN
    }

NUEVO:
    - check_exit_conditions: comprueba take profit, stop loss,
      trailing stop y timeout para cada posición abierta.
"""
from __future__ import annotations

import logging
import time
from typing import Dict
from datetime import datetime, timezone

from . import gmgn
from config.config import CFG

log = logging.getLogger("seller")

# ─── Constantes y umbrales de salida ─────────────────────────────
TAKE_PROFIT = float(CFG.TAKE_PROFIT_PCT or 0) / 100.0
STOP_LOSS = abs(float(CFG.STOP_LOSS_PCT or 0)) / 100.0
TRAILING_STOP = float(CFG.TRAILING_PCT or 0) / 100.0
TIMEOUT_SECONDS = int(CFG.MAX_HOLDING_H or 24) * 3600

# ─── Venta real ─────────────────────────────────────────────────
async def sell(token_addr: str, qty_lamports: int) -> Dict[str, object]:
    if qty_lamports <= 0:
        log.warning("[seller] Qty=0 — orden ignorada")
        return {"signature": "NO_QTY", "route": {}}

    resp = await gmgn.sell(token_addr, qty_lamports)
    return {
        "signature": resp.get("signature"),
        "route": resp.get("route", {}),
    }

# ─── Comprobación de salida de posiciones ────────────────────────
def check_exit_conditions(position: dict, price_now: float) -> str | None:
    """
    Evalúa si una posición debe cerrarse por alguna condición.
    Devuelve el motivo o None si debe seguir abierta.
    """
    buy_price = position.get("buy_price_usd", 0)
    opened_at = position.get("opened_at")
    peak_price = position.get("peak_price", buy_price)

    if not buy_price or not opened_at:
        return None  # Datos insuficientes

    # Convertir fecha ISO a datetime
    opened_dt = datetime.fromisoformat(opened_at).replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - opened_dt).total_seconds()

    pnl_pct = (price_now - buy_price) / buy_price

    # Actualizar peak price (el llamador debe aplicar esto si no se cierra)
    if price_now > peak_price:
        position["peak_price"] = price_now

    # Condiciones de salida
    if pnl_pct >= TAKE_PROFIT:
        return "TAKE_PROFIT"
    elif pnl_pct <= -STOP_LOSS:
        return "STOP_LOSS"
    elif TRAILING_STOP > 0 and price_now <= peak_price * (1 - TRAILING_STOP):
        return "TRAILING_STOP"
    elif age >= TIMEOUT_SECONDS:
        return "TIMEOUT"

    return None