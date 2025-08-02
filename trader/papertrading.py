# trader/papertrading.py
"""
Motor de *paper-trading* (órdenes fantasma) cuando el bot se lanza con
`--dry-run` o `CFG.DRY_RUN = 1`.

Novedad 2025-08-02
──────────────────
• `check_exit_conditions()` usa `utils.price_service.get_price()` con
  `use_gt=True` ⇒ si DexScreener se queda sin datos, hará fallback a
  GeckoTerminal solo para los tokens ya re-encolados.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import time
from typing import Any, Dict, Optional

from config.config import CFG
from utils.time import utc_now
from utils import price_service              # ← NUEVO fallback capa
from fetcher import dexscreener              # se mantiene en buy/sell

log = logging.getLogger("papertrading")

# ───────────────────────── persistencia ─────────────────────────
_DATA_PATH = (
    pathlib.Path(CFG.PROJECT_ROOT if hasattr(CFG, "PROJECT_ROOT") else ".")
    / "data"
    / "paper_portfolio.json"
)
_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

try:
    _PORTFOLIO: Dict[str, Any] = json.loads(_DATA_PATH.read_text())
except Exception:  # noqa: BLE001
    _PORTFOLIO = {}


def _save() -> None:
    try:
        _DATA_PATH.write_text(json.dumps(_PORTFOLIO, indent=2, default=str))
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo guardar porfolio paper-trading: %s", exc)

# ───────────────────────── lógica de compra ─────────────────────
async def buy(address: str, amount_sol: float) -> dict:
    pair = await dexscreener.get_pair(address)           # Dex → ok en compra inicial
    price_usd = float(pair.get("price_usd") or 0.0) if pair else 0.0

    SOL_USD = 160.0  # fijo para simulación
    cost_usd = amount_sol * SOL_USD
    qty_lp = int(amount_sol * 1e9)

    _PORTFOLIO[address] = {
        "qty_lamports": qty_lp,
        "buy_price_usd": price_usd or cost_usd / qty_lp * 1e9,
        "peak_price": price_usd,
        "amount_sol": amount_sol,
        "opened_at": utc_now().isoformat(),
        "closed": False,
    }
    _save()

    log.info("📝 PAPER-BUY %s %.3f SOL", address[:4], amount_sol)
    return {
        "qty_lamports": qty_lp,
        "route": {"quote": {"inAmountUSD": cost_usd}},
    }

# ───────────────────────── lógica de salida ─────────────────────
async def sell(address: str, qty_lamports: int) -> dict:
    entry = _PORTFOLIO.get(address)
    if not entry or entry.get("closed"):
        raise RuntimeError(f"No hay posición activa para {address[:4]}")

    pair = await price_service.get_price(address, use_gt=True)  # fallback si Dex falla
    price_now = float(pair["price_usd"]) if pair else 0.0

    pnl_pct = (
        (price_now - entry["buy_price_usd"]) / entry["buy_price_usd"] * 100
        if entry["buy_price_usd"]
        else 0.0
    )

    entry["closed_at"] = utc_now().isoformat()
    entry["close_price_usd"] = price_now
    entry["pnl_pct"] = pnl_pct
    entry["closed"] = True
    _save()

    sig = f"SIM-{int(time.time() * 1e3)}"
    log.info("📝 PAPER-SELL %s  pnl=%.1f%%  sig=%s", address[:4], pnl_pct, sig)

    return {"signature": sig}

# ───────────────────────── condiciones de salida ────────────────
async def check_exit_conditions(address: str) -> bool:
    entry = _PORTFOLIO.get(address)
    if not entry or entry.get("closed"):
        return False

    # DexScreener ➜ fallback GeckoTerminal solo para tokens con re-intentos
    pair = await price_service.get_price(address, use_gt=True)
    price: float = float(pair["price_usd"]) if pair else 0.0

    buy_price = entry.get("buy_price_usd") or 0.0
    peak_price = entry.get("peak_price") or price

    # actualizar peak
    if price > peak_price:
        entry["peak_price"] = price
        peak_price = price
        _save()

    # condiciones
    pnl = (price - buy_price) / buy_price * 100 if buy_price else 0.0
    trailing = peak_price * (1 - CFG.TRAILING_PCT / 100.0)
    timeout = False
    try:
        opened_at = dt.datetime.fromisoformat(entry["opened_at"])
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=dt.timezone.utc)
        timeout = (utc_now() - opened_at).total_seconds() > CFG.MAX_HOLDING_H * 3600
    except Exception:
        timeout = False

    # evaluaciones
    if price <= 0:
        return False
    if pnl >= CFG.TAKE_PROFIT_PCT:
        return True
    if pnl <= -CFG.STOP_LOSS_PCT:
        return True
    if price <= trailing:
        return True
    if timeout:
        return True

    return False
