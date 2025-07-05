"""
trader.papertrading
────────────────────
Motor de *paper-trading* (órdenes fantasma) usado cuando el bot se lanza
con el flag `--dry-run` o `CFG.DRY_RUN=1`.

• Simula la compra/venta y mantiene un “porfolio” en memoria ‒y opcional
  persistencia ligera a disco‒ para poder revisar resultados.
• Devuelve objetos compatibles con los que espera `run_bot.py`
  (`qty_lamports`, `route.quote.inAmountUSD`, `signature`, …).
"""

from __future__ import annotations

import json
import pathlib
import time
import datetime as dt
import logging
from typing import Dict, Any

from config.config import CFG
from fetcher import dexscreener
from utils.time import utc_now

log = logging.getLogger("papertrading")

# ───────────────────────── persistencia ─────────────────────────
_DATA_PATH = pathlib.Path(CFG.PROJECT_ROOT if hasattr(CFG, "PROJECT_ROOT") else ".") \
            / "data" / "paper_portfolio.json"
_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

# Carga porfolio previo (si existe)
try:
    _PORTFOLIO: Dict[str, Any] = json.loads(_DATA_PATH.read_text())
except Exception:       # noqa: BLE001
    _PORTFOLIO = {}


def _save() -> None:
    try:
        _DATA_PATH.write_text(json.dumps(_PORTFOLIO, indent=2, default=str))
    except Exception as exc:     # noqa: BLE001
        log.warning("No se pudo guardar porfolio paper-trading: %s", exc)


# ───────────────────────── helpers público ──────────────────────
async def buy(address: str, amount_sol: float) -> dict:
    """
    Simula la compra.  Calcula el precio USD vía DexScreener.
    Devuelve estructura equivalente a `trader.buyer.buy`.
    """
    pair = await dexscreener.get_pair(address)
    if not pair or not pair.get("price_usd"):
        raise RuntimeError(f"Precio no disponible para {address[:4]}")

    price_usd = float(pair["price_usd"])
    # — simplificación: 1 SOL ≈ 160 USD (ajusta si quieres) —
    sol_usd   = 160.0
    cost_usd  = amount_sol * sol_usd
    qty_lp    = int(amount_sol * 1e9)      # lamports simbólicos

    _PORTFOLIO[address] = {
        "qty_lamports": qty_lp,
        "buy_price_usd": price_usd,
        "amount_sol": amount_sol,
        "opened_at": utc_now().isoformat(),
    }
    _save()

    log.info("📝 PAPER-BUY %s  %.3f SOL (≈%.0f USD)  price=%.6f",
             address[:4], amount_sol, cost_usd, price_usd)

    return {
        "qty_lamports": qty_lp,
        "route": {
            "quote": {"inAmountUSD": cost_usd}
        }
    }


async def sell(address: str, qty_lamports: int) -> dict:
    """
    Simula la venta.  Calcula PnL % y elimina la posición.
    Devuelve dict con `signature` ficticia.
    """
    entry = _PORTFOLIO.get(address)
    if not entry:
        raise RuntimeError(f"No hay posición paper para {address[:4]}")

    pair = await dexscreener.get_pair(address)
    price_now = float(pair["price_usd"]) if pair else 0.0

    pnl_pct = ((price_now - entry["buy_price_usd"])
               / entry["buy_price_usd"] * 100) if entry["buy_price_usd"] else 0.0
    entry["closed_at"]  = utc_now().isoformat()
    entry["close_price_usd"] = price_now
    entry["pnl_pct"]    = pnl_pct
    entry["closed"]     = True
    _save()

    sig = f"SIM-{int(time.time()*1e3)}"
    log.info("📝 PAPER-SELL %s  pnl=%.1f%%  sig=%s",
             address[:4], pnl_pct, sig)

    return {"signature": sig}
