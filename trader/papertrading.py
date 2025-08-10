# trader/papertrading.py
"""
Motor de *paper-trading* (órdenes fantasma) cuando el bot se ejecuta con
`--dry-run` o `CFG.DRY_RUN = 1`.

Mejoras 2025-08-10:
──────────────────
• Bloquea direcciones no-Solana (0x…).
• Todas las consultas de precio fuerzan use_gt=True (Dex → Birdeye → GT → native×SOL).
• Reintento corto al consultar precio.
• Cierre *seguro*: si no hay precio de salida, usa buy_price como fallback (PnL 0%),
  evitando cierres con `close_price_usd=0.0` y PnL −100% ficticio.
• Si no hay precio en `check_exit_conditions`, no se fuerza venta salvo por TIMEOUT.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import pathlib
import time
from typing import Any, Dict

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


# ───────────────────── utilidades locales ──────────────────────
def _is_solana_address(addr: str) -> bool:
    """Filtro defensivo: descarta EVM (0x…) y longitudes extrañas."""
    if not addr or addr.startswith("0x"):
        return False
    return 30 <= len(addr) <= 50  # rango típico base58 de mints SOL


async def _get_price_usd_with_retry(address: str, *, retries: int = 1, delay: float = 2.0) -> float:
    """Precio USD con use_gt=True, reintentando si es necesario."""
    price = await price_service.get_price_usd(address, use_gt=True)
    if price:
        try:
            return float(price)
        except Exception:
            pass
    for _ in range(retries):
        await asyncio.sleep(delay)
        price = await price_service.get_price_usd(address, use_gt=True)
        if price:
            try:
                return float(price)
            except Exception:
                return 0.0
    return 0.0


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
    # 0️⃣ Validación de red
    if not _is_solana_address(address):
        raise ValueError(f"[papertrading] Dirección no Solana bloqueada: {address!r}")

    # 1️⃣ Precio USD (mejor-esfuerzo, forzando use_gt=True)
    price_usd = await price_service.get_price_usd(address, use_gt=True)
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
        "token_address": address,
    }
    _save()

    # 4️⃣ Reintento diferido si no había precio
    if price_usd in (None, 0.0):
        asyncio.create_task(_retry_fill_buy_price(address))

    log.info("📝 PAPER-BUY %s…  %.3f SOL (≈ %.2f USD)", address[:4], amount_sol, cost_usd)
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
        price = await price_service.get_price_usd(address, use_gt=True)
        if price:
            entry = _PORTFOLIO.get(address)
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

    if not _is_solana_address(address):
        log.error("[papertrading] Venta bloqueada: address no Solana %r", address)
        sig = f"SIM-{int(time.time()*1e3)}"
        return {"signature": sig, "error": "INVALID_ADDRESS"}

    # Precio actual con reintento corto
    price_now = await _get_price_usd_with_retry(address, retries=1, delay=2.0)

    # Cierre *seguro*: si no hay precio, usa buy_price como fallback (PnL 0%)
    if price_now <= 0.0:
        bp = float(entry.get("buy_price_usd") or 0.0)
        if bp > 0.0:
            log.warning(
                "[papertrading] Precio de cierre no disponible para %s…; "
                "uso buy_price como fallback.",
                address[:4],
            )
            price_now = bp
        else:
            log.error(
                "[papertrading] Sin precio de compra ni precio actual para %s…; "
                "close_price_usd=0.0; pnl_pct=0.0",
                address[:4],
            )
            price_now = 0.0

    pnl_pct = (
        ((price_now - float(entry.get("buy_price_usd") or 0.0)) / float(entry.get("buy_price_usd") or 1.0)) * 100.0
        if float(entry.get("buy_price_usd") or 0.0) > 0.0
        else 0.0
    )

    entry.update(
        {
            "closed_at": utc_now().isoformat(),
            "close_price_usd": float(price_now),
            "pnl_pct": float(pnl_pct),
            "closed": True,
        }
    )
    _save()

    sig = f"SIM-{int(time.time()*1e3)}"
    log.info(
        "📝 PAPER-SELL %s…  close=%.6f USD  PnL=%.2f%%  sig=%s",
        address[:4],
        price_now,
        pnl_pct,
        sig,
    )
    return {"signature": sig}


# ─── evaluación de salida (TP/SL/Trailing/Timeout) ────────────
async def check_exit_conditions(address: str) -> bool:  # noqa: C901
    """
    Devuelve True si **alguna** condición de salida se cumple.
    Si no hay precio, no fuerza salida por precio; solo se considerará TIMEOUT.
    """
    entry = _PORTFOLIO.get(address)
    if not entry or entry.get("closed"):
        return False

    # Precio actual (un intento; no cerramos por 'price<=0', ver abajo)
    pair = await price_service.get_price(address, use_gt=True)
    price = float(pair["price_usd"]) if pair and "price_usd" in pair else 0.0

    buy_price = float(entry.get("buy_price_usd") or 0.0)
    peak_price = float(entry.get("peak_price") or (price if price > 0 else buy_price))

    # Actualiza pico sólo si hay precio válido
    if price > 0.0 and price > peak_price:
        entry["peak_price"] = peak_price = price
        _save()

    # PnL en %
    pnl = ((price - buy_price) / buy_price * 100.0) if (buy_price > 0.0 and price > 0.0) else 0.0
    trailing_lvl = peak_price * (1 - float(CFG.TRAILING_PCT or 0.0) / 100.0)

    # TIMEOUT (siempre aplicable)
    try:
        opened_at = dt.datetime.fromisoformat(entry["opened_at"])
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=dt.timezone.utc)
        timeout = (utc_now() - opened_at).total_seconds() > float(CFG.MAX_HOLDING_H or 0.0) * 3600.0
    except Exception:
        timeout = False

    # Condiciones basadas en precio solo si hay precio válido
    tp_hit = (price > 0.0) and (pnl >= float(CFG.TAKE_PROFIT_PCT or 0.0))
    sl_hit = (price > 0.0) and (pnl <= -float(CFG.STOP_LOSS_PCT or 0.0))
    tr_hit = (price > 0.0) and (float(CFG.TRAILING_PCT or 0.0) > 0.0) and (price <= trailing_lvl)

    # Nota: eliminamos el antiguo 'price <= 0' como disparador de salida.
    return any([tp_hit, sl_hit, tr_hit, timeout])


# ─── exportación mínima ────────────────────────────────────────
__all__ = ["buy", "sell", "check_exit_conditions"]
