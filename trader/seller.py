# trader/seller.py
"""
Interfaz unificada para:
    • Enviar la orden real de venta (`gmgn.sell`)
    • Evaluar las condiciones de salida (TP / SL / Trailing / Timeout)
    • Obtener el precio actual ABSTRAYÉNDOSE de la fuente concreta:
        DexScreener → Birdeye → GeckoTerminal → conversión price_native→USD

2025-08-09
──────────
• `get_current_price()` ahora llama a
  `utils.price_service.get_price_usd(..., use_gt=True)` para forzar ruta
  completa en modo real, igual que en papertrading.
• Si esa función devuelve None, se reporta 0.0 (sin precio).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict

from config.config import CFG
from utils import price_service
from . import gmgn  # SDK local

log = logging.getLogger("seller")

# ─── Umbrales de salida (config) ──────────────────────────────────
TAKE_PROFIT     = float(CFG.TAKE_PROFIT_PCT or 0) / 100.0
STOP_LOSS       = abs(float(CFG.STOP_LOSS_PCT or 0)) / 100.0
TRAILING_STOP   = float(CFG.TRAILING_PCT or 0) / 100.0
TIMEOUT_SECONDS = int(CFG.MAX_HOLDING_H or 24) * 3600


# ─── Precio actual (Dex → Birdeye → GT → price_native→USD) ────────
async def get_current_price(token_addr: str) -> float:
    """
    Devuelve el precio USD del token forzando la ruta completa de
    fallbacks: DexScreener → Birdeye → GeckoTerminal → native×SOL.

    Returns
    -------
    float
        Precio en USD o 0.0 si no se pudo obtener.
    """
    price = await price_service.get_price_usd(token_addr, use_gt=True)
    return float(price) if price else 0.0


# ─── Venta real ───────────────────────────────────────────────────
async def sell(token_addr: str, qty_lamports: int) -> Dict[str, object]:
    """
    Ejecuta la orden de venta con gmgn. Devuelve firma y ruta
    o un código especial si qty==0.
    """
    if qty_lamports <= 0:
        log.warning("[seller] Qty=0 — orden ignorada")
        return {"signature": "NO_QTY", "route": {}}

    resp = await gmgn.sell(token_addr, qty_lamports)
    return {
        "signature": resp.get("signature"),
        "route": resp.get("route", {}),
    }


# ─── Evaluación de condiciones de salida ──────────────────────────
def check_exit_conditions(position: dict, price_now: float) -> str | None:
    """
    Devuelve una cadena con el *motivo* de salida o None si la posición
    debe permanecer abierta.

    Motivos: "TAKE_PROFIT", "STOP_LOSS", "TRAILING_STOP", "TIMEOUT"
    """
    buy_price  = position.get("buy_price_usd", 0.0)
    opened_at  = position.get("opened_at")
    peak_price = position.get("peak_price", buy_price)

    if not buy_price or not opened_at:
        return None  # datos insuficientes

    # edad de la posición
    opened_dt = datetime.fromisoformat(opened_at).replace(tzinfo=timezone.utc)
    age_sec = (datetime.now(timezone.utc) - opened_dt).total_seconds()

    # rentabilidad actual
    pnl_pct = (price_now - buy_price) / buy_price if buy_price else 0.0

    # actualizar máximo histórico
    if price_now > peak_price:
        position["peak_price"] = price_now
        peak_price = price_now

    # reglas de salida
    if pnl_pct >= TAKE_PROFIT:
        return "TAKE_PROFIT"
    if pnl_pct <= -STOP_LOSS:
        return "STOP_LOSS"
    if TRAILING_STOP > 0 and price_now <= peak_price * (1 - TRAILING_STOP):
        return "TRAILING_STOP"
    if age_sec >= TIMEOUT_SECONDS:
        return "TIMEOUT"

    return None
