# trader/papertrading.py
"""
Motor de *paper-trading* (órdenes fantasma) cuando el bot se ejecuta con
`--dry-run` o `CFG.DRY_RUN = 1`.

Mejoras 2025-08-02
──────────────────
• Precio SOL dinámico vía CoinGecko (utils.sol_price).
• Obtención del precio USD del token con utils.price_service:
    DexScreener → GeckoTerminal → price_native×SOL_USD.
• Si el precio no está disponible al abrir la posición, se agenda
  un reintento asíncrono para rellenar `buy_price_usd` y `peak_price`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import pathlib
import time
from typing import Any, Dict, Optional

from config.config import CFG
from utils.time import utc_now
from utils import price_service, sol_price

log = logging.getLogger("papertrading")

# ───────────────────────── persistencia ─────────────────────────
_DATA_PATH = (
    pathlib.Path(getattr(CFG, "PROJECT_ROOT", ".")) / "data" / "paper_portfolio.json"
)
_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

try:
    _PORTFOLIO: Dict[str, Any] = json.loads(_DATA_PATH.read_text())
except Exception:  # noqa: BLE001
    _PORTFOLIO = {}


def _save() -> None:
    """Graba `_PORTFOLIO` en disco (best-effort)."""
    try:
        _DATA_PATH.write_text(json.dumps(_PORTFOLIO, indent=2, default=str))
    except Exception as exc:  # noqa: BLE001
        log.warning("[papertrading] no se pudo guardar portfolio: %s", exc)

# ─── lógica de compra ──────────────────────────────────────────
async def buy(
    address: str,
    amount_sol: float,
    *,
    price_hint: float | None = None,
) -> dict:
    """
    Registra una posición simulada.

    Parameters
    ----------
    address : str
        Mint address del token (Solana).
    amount_sol : float
        Importe simulado en SOL.
    price_hint : float | None, default None
        Valor precio-unidad USD opcional facilitado por el orquestador.
    """
    # 1️⃣ Precio USD (mejor-esfuerzo)
    price_usd = await price_service.get_price_usd(address)
    if price_usd in (None, 0.0) and price_hint not in (None, 0.0):
        price_usd = float(price_hint)

    # 2️⃣ Coste de referencia en USD (para el log)
    sol_ref = await sol_price.get_sol_usd() or 0.0
    cost_usd = amount_sol * sol_ref

    # 3️⃣ Alta de la posición
    qty_lp = int(amount_sol * 1e9)  # simulamos lamports de token
    _PORTFOLIO[address] = {
        "qty_lamports": qty_lp,
        "buy_price_usd": float(price_usd or 0.0),
        "peak_price": float(price_usd or 0.0),
        "amount_sol": amount_sol,
        "opened_at": utc_now().isoformat(),
        "closed": False,
    }
    _save()

    # 4️⃣ Reintento diferido si no había precio
    if price_usd in (None, 0.0):
        asyncio.create_task(_retry_fill_buy_price(address))

    log.info("📝 PAPER-BUY %s  %.3f SOL (≈ %.2f USD)", address[:4], amount_sol, cost_usd)
    return {
        "qty_lamports": qty_lp,
        "signature": f"SIM-{int(time.time()*1e3)}",
        "route": {},
        "buy_price_usd": float(price_usd or 0.0),
        "peak_price": float(price_usd or 0.0),
    }

# ─── rellenar precio tras la compra ────────────────────────────
async def _retry_fill_buy_price(
    address: str,
    *,
    tries: int = 3,
    delay: int = 8,
) -> None:
    """Intenta rellenar `buy_price_usd`/`peak_price` si quedaron a 0."""
    for attempt in range(1, tries + 1):
        await asyncio.sleep(delay)
        price = await price_service.get_price_usd(address)
        if price:
            entry = _PORTFOLIO.get(address)
            # ★ aceptar 0.0 *o* None
            if entry and entry.get("buy_price_usd") in (0.0, None):
                entry["buy_price_usd"] = entry["peak_price"] = float(price)
                _save()
                log.info(
                    "[papertrading] buy_price_usd actualizado a %.6f USD (retry %d)",
                    price,
                    attempt,
                )
            break

# ─── lógica de salida ─────────────────────────────────────────
async def sell(address: str, qty_lamports: int) -> dict:
    entry = _PORTFOLIO.get(address)
    if not entry or entry.get("closed"):
        raise RuntimeError(f"No hay posición activa para {address[:4]}")

    price_now = await price_service.get_price_usd(address) or 0.0
    pnl_pct = (
        (price_now - entry["buy_price_usd"]) / entry["buy_price_usd"] * 100
        if entry["buy_price_usd"]
        else 0.0
    )

    entry.update(
        {
            "closed_at": utc_now().isoformat(),
            "close_price_usd": price_now,
            "pnl_pct": pnl_pct,
            "closed": True,
        }
    )
    _save()

    sig = f"SIM-{int(time.time()*1e3)}"
    log.info(
        "📝 PAPER-SELL %s  close=%.6f USD  PnL=%.2f%%  sig=%s",
        address[:4],
        price_now,
        pnl_pct,
        sig,
    )
    return {"signature": sig}

# ─── evaluación de salida (TP/SL/Trailing/Timeout) ────────────
async def check_exit_conditions(address: str) -> bool:  # noqa: C901
    entry = _PORTFOLIO.get(address)
    if not entry or entry.get("closed"):
        return False

    pair = await price_service.get_price(address, use_gt=True)
    price = float(pair["price_usd"]) if pair else 0.0

    buy_price  = entry.get("buy_price_usd") or 0.0
    peak_price = entry.get("peak_price") or price

    if price > peak_price:
        entry["peak_price"] = peak_price = price
        _save()

    pnl          = (price - buy_price) / buy_price * 100 if buy_price else 0.0
    trailing_lvl = peak_price * (1 - CFG.TRAILING_PCT / 100.0)

    # timeout
    try:
        opened_at = dt.datetime.fromisoformat(entry["opened_at"])
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=dt.timezone.utc)
        timeout = (utc_now() - opened_at).total_seconds() > CFG.MAX_HOLDING_H * 3600
    except Exception:
        timeout = False

    return any(
        [
            price <= 0,
            pnl >= CFG.TAKE_PROFIT_PCT,
            pnl <= -CFG.STOP_LOSS_PCT,
            price <= trailing_lvl,
            timeout,
        ]
    )

# ─── exportación mínima ────────────────────────────────────────
__all__ = ["buy", "sell", "check_exit_conditions"]
