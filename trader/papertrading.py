# trader/papertrading.py
"""
Motor de *paper-trading* (órdenes fantasma) cuando el bot se ejecuta con
`--dry-run` o `CFG.DRY_RUN = 1`.

Mejoras 2025-08-10:
──────────────────
• Bloquea direcciones no-Solana (0x…).
• Calcula buy_price_usd con prioridad: Jupiter → SOL/USD (estimación) → hint DexScreener.
• Devuelve y persiste `price_source` para trazar el origen del precio.
• Reintento corto para rellenar el precio de compra si inicialmente no estaba disponible.
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
from typing import Any, Dict, Optional

from config.config import CFG
from utils.time import utc_now
from utils import price_service
from fetcher import jupiter_price

log = logging.getLogger("papertrading")

SOL_MINT = "So11111111111111111111111111111111111111112"

async def _resolve_buy_price_usd(
    token_mint: str,
    amount_sol: float,
    tokens_received: Optional[float],
    ds_price_usd: Optional[float] = None,
) -> tuple[float, str]:
    # 1) Intenta precio directo en Jupiter
    p = await jupiter_price.get_usd_price(token_mint)
    if p is not None and p > 0:
        return float(p), "jupiter"
    # 2) Estimar con SOL/USD si sabemos cuántos tokens recibimos
    sol_usd = await jupiter_price.get_usd_price(SOL_MINT)
    if sol_usd and sol_usd > 0 and tokens_received and tokens_received > 0:
        return float((amount_sol * sol_usd) / tokens_received), "sol_estimate"
    # 3) Hint (DexScreener) si venía del orquestador
    if ds_price_usd and ds_price_usd > 0:
        return float(ds_price_usd), "dexscreener"
    # 4) Último recurso
    log.warning("[buy] No pude resolver buy_price_usd para %s; guardo 0.0", token_mint[:6])
    return 0.0, "fallback0"


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


# ─── lógica de compra ──────────────────────────────────────────
async def buy(
    address: str,
    amount_sol: float,
    *,
    price_hint: float | None = None,
    token_mint: str | None = None,
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
    token_mint : str | None
        Mint preferente (si ya lo tienes normalizado), si no se usa `address`.
    """
    # 0️⃣ Validación de red
    if not _is_solana_address(address):
        raise ValueError(f"[papertrading] Dirección no Solana bloqueada: {address!r}")

    # 1️⃣ Resolver precio de compra con trazabilidad
    tokens_received = None  # en paper no sabemos la cantidad exacta recibida
    mint_key = token_mint or address
    buy_price_usd, price_src = await _resolve_buy_price_usd(
        token_mint=mint_key,
        amount_sol=amount_sol,
        tokens_received=tokens_received,
        ds_price_usd=price_hint,
    )

    # 2️⃣ Alta de la posición en el JSON
    qty_lp = int(amount_sol * 1e9)  # simulamos lamports de token
    _PORTFOLIO[mint_key] = {
        "qty_lamports": qty_lp,
        "buy_price_usd": float(buy_price_usd),
        "peak_price": float(buy_price_usd),
        "amount_sol": amount_sol,
        "opened_at": utc_now().isoformat(),
        "closed": False,
        "token_address": mint_key,
        "price_source": price_src,  # trazabilidad
    }
    _save()

    # 3️⃣ Reintento diferido si no había precio (por si Jupiter indexa después)
    if buy_price_usd in (None, 0.0):
        asyncio.create_task(_retry_fill_buy_price(mint_key))

    log.info(
        "[papertrading] BUY %s amount_sol=%.3f price_usd=%.8g src=%s",
        mint_key[:6], amount_sol, buy_price_usd, price_src,
    )
    return {
        "qty_lamports": qty_lp,
        "signature": f"SIM-{int(time.time()*1e3)}",
        "route": {},
        "buy_price_usd": float(buy_price_usd),
        "peak_price": float(buy_price_usd),
        "price_source": price_src,   # **imprescindible** para run_bot
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
        price = await jupiter_price.get_usd_price(address)
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

    # Precio actual en MODO CRÍTICO (ignora cache negativa) + reintento corto
    price_now = await price_service.get_price_usd(address, use_gt=True, critical=True)
    if (price_now is None) or (price_now <= 0.0):
        await asyncio.sleep(2.0)
        price_now = await price_service.get_price_usd(address, use_gt=True, critical=True)

    # Cierre *seguro*: si aún no hay precio, usa buy_price (PnL 0%)
    if not price_now or price_now <= 0.0:
        bp = float(entry.get("buy_price_usd") or 0.0)
        if bp > 0.0:
            log.warning(
                "[papertrading] Precio de cierre no disponible para %s…; uso buy_price como fallback.",
                address[:4],
            )
            price_now = bp
        else:
            log.error(
                "[papertrading] Sin precio de compra ni precio actual para %s…; close_price_usd=0.0; pnl_pct=0.0",
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

    # Precio actual (solo precio; crítico para saltarse caché negativa en cierres)
    price_val = await price_service.get_price_usd(address, use_gt=True, critical=True)
    price = float(price_val or 0.0)

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

    # Nota: no se fuerza salida por 'price<=0'; si no hay precio, solo TIMEOUT.
    return any([tp_hit, sl_hit, tr_hit, timeout])


# ─── exportación mínima ────────────────────────────────────────
__all__ = ["buy", "sell", "check_exit_conditions"]
