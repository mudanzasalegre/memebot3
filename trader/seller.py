# trader/seller.py
"""
Interfaz unificada para salidas en modo REAL:
    • Enviar la orden real de venta (`gmgn.sell`)
    • Evaluar las condiciones de salida (TP / SL / Trailing / Timeout)
    • Obtener el precio actual abstrayéndose de la fuente concreta:
        DexScreener → Birdeye → GeckoTerminal → conversión price_native→USD
    • Generar un snapshot de cierre con PnL coherente incluso si
      no se pudo obtener el precio (fallback = buy_price)

2025-08-10
──────────
Cambios clave:
• Filtro defensivo de direcciones EVM (0x…) para evitar ventas fuera de Solana.
• get_current_price(): usa critical=True (ignora caché negativa) y reintenta 1 vez.
• safe_close_snapshot(): usa critical=True, reintento breve y fallback al buy_price
  para no falsear el PnL.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from config.config import CFG
from utils import price_service
from . import gmgn  # SDK local

log = logging.getLogger("seller")

# ─── Umbrales de salida (config) ──────────────────────────────────
TAKE_PROFIT_PCT   = float(CFG.TAKE_PROFIT_PCT or 0.0)
STOP_LOSS_PCT     = float(CFG.STOP_LOSS_PCT or 0.0)
TRAILING_PCT      = float(CFG.TRAILING_PCT or 0.0)
MAX_HOLDING_H     = float(CFG.MAX_HOLDING_H or 24)

TAKE_PROFIT       = TAKE_PROFIT_PCT / 100.0
STOP_LOSS         = abs(STOP_LOSS_PCT) / 100.0
TRAILING_STOP     = TRAILING_PCT / 100.0
TIMEOUT_SECONDS   = int(MAX_HOLDING_H * 3600)


# ─── Utilidades ───────────────────────────────────────────────────
def _is_solana_address(addr: str) -> bool:
    """Check muy simple: descarta EVM (0x…) y longitudes extrañas."""
    if not addr or addr.startswith("0x"):
        return False
    # Direcciones de mint de Solana suelen estar ~32–44 chars base58.
    return 30 <= len(addr) <= 50


# ─── Precio actual (Dex → Birdeye → GT → price_native→USD) ────────
async def get_current_price(token_addr: str) -> float:
    """
    Devuelve el precio USD del token forzando la ruta completa de fallbacks:
    DexScreener → Birdeye → GeckoTerminal → native×SOL.

    Usa critical=True para ignorar caché negativa en cierres.

    Retorna:
        float: precio en USD o 0.0 si no se pudo obtener.
    """
    if not _is_solana_address(token_addr):
        log.error("[seller] Dirección no Solana detectada: %r", token_addr)
        return 0.0

    # Primer intento (modo crítico)
    price = await price_service.get_price_usd(token_addr, use_gt=True, critical=True)
    if price:
        try:
            return float(price)
        except Exception:
            pass

    # Reintento breve (APIs pueden dar null/timeout puntuales)
    await asyncio.sleep(2.0)
    price = await price_service.get_price_usd(token_addr, use_gt=True, critical=True)
    if price:
        try:
            return float(price)
        except Exception:
            return 0.0

    return 0.0


# ─── Venta real ───────────────────────────────────────────────────
async def sell(token_addr: str, qty_lamports: int) -> Dict[str, object]:
    """
    Ejecuta la orden de venta con gmgn. Devuelve firma y ruta
    o un código especial si qty==0 o si la dirección no es Solana.
    """
    if not _is_solana_address(token_addr):
        log.error("[seller] Venta bloqueada: address no Solana %r", token_addr)
        return {"signature": "INVALID_ADDRESS", "route": {}, "ok": False}

    if qty_lamports <= 0:
        log.warning("[seller] Qty=0 — orden ignorada")
        return {"signature": "NO_QTY", "route": {}, "ok": False}

    try:
        resp = await gmgn.sell(token_addr, qty_lamports)
        return {
            "signature": resp.get("signature"),
            "route": resp.get("route", {}),
            "ok": True,
        }
    except Exception as e:
        log.exception("[seller] Error vendiendo %s: %s", token_addr, e)
        return {"signature": "ERROR", "route": {}, "ok": False, "error": str(e)}


# ─── Evaluación de condiciones de salida ──────────────────────────
def check_exit_conditions(position: dict, price_now: float) -> Optional[str]:
    """
    Devuelve una cadena con el *motivo* de salida o None si la posición
    debe permanecer abierta.

    Motivos: "TAKE_PROFIT", "STOP_LOSS", "TRAILING_STOP", "TIMEOUT"
    """
    buy_price  = float(position.get("buy_price_usd", 0.0) or 0.0)
    opened_at  = position.get("opened_at")
    peak_price = float(position.get("peak_price", buy_price) or buy_price)

    if not buy_price or not opened_at:
        return None  # datos insuficientes

    # edad de la posición
    try:
        opened_dt = datetime.fromisoformat(opened_at)
        if opened_dt.tzinfo is None:
            opened_dt = opened_dt.replace(tzinfo=timezone.utc)
        age_sec = (datetime.now(timezone.utc) - opened_dt).total_seconds()
    except Exception:
        # Si el timestamp llega malformado, no forzamos cierre por tiempo.
        age_sec = 0

    # rentabilidad actual
    pnl_pct = (price_now - buy_price) / buy_price if buy_price else 0.0

    # actualizar máximo histórico
    if price_now > peak_price:
        position["peak_price"] = price_now
        peak_price = price_now

    # reglas de salida
    if TAKE_PROFIT > 0 and pnl_pct >= TAKE_PROFIT:
        return "TAKE_PROFIT"
    if STOP_LOSS > 0 and pnl_pct <= -STOP_LOSS:
        return "STOP_LOSS"
    if TRAILING_STOP > 0 and price_now <= peak_price * (1 - TRAILING_STOP):
        return "TRAILING_STOP"
    if TIMEOUT_SECONDS > 0 and age_sec >= TIMEOUT_SECONDS:
        return "TIMEOUT"

    return None


# ─── Snapshot seguro de cierre ────────────────────────────────────
async def safe_close_snapshot(position: dict, exit_reason: str) -> dict:
    """
    Construye los campos de cierre con precio de salida *seguro*:
      - Intenta obtener precio actual en modo crítico; si no hay, reintenta 1 vez.
      - Si sigue faltando precio, usa buy_price como fallback (evita PnL -100% ficticio).
      - Calcula pnl_pct de forma coherente.
      - Sella closed_at y exit_reason.

    Devuelve un dict con:
      close_price_usd, pnl_pct, closed_at, exit_reason
    (Listo para ser persistido junto a la posición.)
    """
    token_addr = position.get("token_address") or position.get("address") or ""
    buy_price  = float(position.get("buy_price_usd", 0.0) or 0.0)

    # Precio en MODO CRÍTICO (ignora caché negativa)
    price_now = await price_service.get_price_usd(token_addr, use_gt=True, critical=True)
    if price_now is None or price_now <= 0.0:
        await asyncio.sleep(2.0)
        price_now = await price_service.get_price_usd(token_addr, use_gt=True, critical=True)

    # Fallback para no distorsionar con -100 % ficticio
    if price_now is None or price_now <= 0.0:
        if buy_price > 0.0:
            log.warning(
                "[seller] Precio de cierre no disponible para %s. Se usa buy_price como fallback.",
                token_addr[:6],
            )
            price_now = buy_price
        else:
            # Último fallback: 0.0 (raro; mantén logs para depurar)
            log.error(
                "[seller] Sin precio de compra ni precio actual para %s. close_price_usd=0.0; pnl_pct=0.0",
                token_addr[:6],
            )
            price_now = 0.0

    pnl_pct = 0.0 if buy_price <= 0 else ((float(price_now) - buy_price) / buy_price) * 100.0
    closed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return {
        "close_price_usd": float(price_now),
        "pnl_pct": float(pnl_pct),
        "closed_at": closed_at,
        "exit_reason": exit_reason,
    }
