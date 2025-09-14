# trader/seller.py
"""
Interfaz unificada para salidas en modo REAL:
    • Enviar la orden real de venta priorizando Jupiter (si hay router);
      fallback a gmgn.sell solo con liquidez suficiente.
    • Evaluar las condiciones de salida (TP parcial / SL / Trailing / Timeout / No-Expansion)
    • Señales extra: EARLY_DROP y LIQUIDITY_CRUSH (alineadas con run_bot)
    • Obtener el precio actual abstrayéndose de la fuente concreta:
        DexScreener → Birdeye → GeckoTerminal → conversión price_native→USD
    • Generar un snapshot de cierre con PnL coherente incluso si
      no se pudo obtener el precio (fallback = buy_price)

2025-08-28
──────────
• Prioridad Jupiter en cierre (si hay `fetcher.jupiter_router`): get_quote→execute_swap.
  Fallback a gmgn.sell solo si liquidity_usd ≥ CFG.MIN_LIQUIDITY_USD (si no, requeue/skip).
• Nuevo motivo de salida "NO_EXPANSION": si age≥1h y pnl_pct ≤ NO_EXPANSION_MAX_PCT (env, por defecto 0.0).
• Timeout con extensión condicional: si pnl_pct ≥ TRAILING_PCT permite alargar hasta MAX_HARD_HOLD_H (env, por defecto 4h).
• TP parcial: al alcanzar TAKE_PROFIT_PCT vende WIN_PCT (env; por defecto 0.30) y mantiene trailing sobre el resto.
• SL/TP/Trailing ajustados a CFG.* y robustecidos.

2025-08-23
──────────
• Alineación de variables .env con run_bot (early-drop y liquidity-crush).
• `check_exit_conditions(...)` acepta tanto `buy_liquidity_usd` como `liq_at_buy_usd`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from config.config import CFG
from utils.time import parse_iso_utc
from utils import price_service
from fetcher import jupiter_price

# Router Jupiter opcional (para cuotas/ejecución de swap)
try:
    # Se espera: get_quote(input_mint, output_mint, amount_tokens) y execute_swap(quote)
    from fetcher import jupiter_router as jupiter  # type: ignore
    _JUP_ROUTER_AVAILABLE = True
except Exception:
    jupiter = None  # type: ignore
    _JUP_ROUTER_AVAILABLE = False

# gmgn SDK local
from . import gmgn  # type: ignore

log = logging.getLogger("seller")

SOL_MINT = "So11111111111111111111111111111111111111112"

# ─── Umbrales de salida (config base) ───────────────────────────────────────
TAKE_PROFIT_PCT   = float(CFG.TAKE_PROFIT_PCT or 0.0)
STOP_LOSS_PCT     = float(CFG.STOP_LOSS_PCT or 0.0)
TRAILING_PCT      = float(CFG.TRAILING_PCT or 0.0)
MAX_HOLDING_H     = float(CFG.MAX_HOLDING_H or 24)

TAKE_PROFIT       = TAKE_PROFIT_PCT / 100.0
STOP_LOSS         = abs(STOP_LOSS_PCT) / 100.0
TRAILING_STOP     = TRAILING_PCT / 100.0
TIMEOUT_SECONDS   = int(MAX_HOLDING_H * 3600)

# TP parcial (fracción de la posición a realizar)
try:
    WIN_PCT = float(getattr(CFG, "WIN_PCT", 0.30))
except Exception:
    WIN_PCT = 0.30
WIN_PCT = min(max(WIN_PCT, 0.05), 0.95)  # clamp 5%..95%

# Extensión máxima dura (si va muy en verde)
try:
    MAX_HARD_HOLD_H = float(os.getenv("MAX_HARD_HOLD_H", "4"))
except Exception:
    MAX_HARD_HOLD_H = 4.0
HARD_TIMEOUT_SECONDS = int(MAX_HARD_HOLD_H * 3600)

# No-Expansion: cierre temprano a 1h si PnL ≤ umbral (por defecto 0%)
try:
    NO_EXPANSION_MAX_PCT = float(os.getenv("NO_EXPANSION_MAX_PCT", "0.0"))
except Exception:
    NO_EXPANSION_MAX_PCT = 0.0
NO_EXPANSION_MAX_FRAC = NO_EXPANSION_MAX_PCT / 100.0

# ─── Señales extra (alineadas con run_bot; con fallbacks) ───────────────────
# Early drop:
_EARLY_DROP_PCT = os.getenv("KILL_EARLY_DROP_PCT")
if _EARLY_DROP_PCT is None:
    _EARLY_DROP_PCT = os.getenv("EARLY_DROP_PCT", "45")
EARLY_DROP_PCT = float(_EARLY_DROP_PCT)

# Ventana early-drop en segundos (o fallback minutos)
if (ew_s := os.getenv("KILL_EARLY_WINDOW_S")) is not None:
    EARLY_WINDOW_S = int(ew_s)
else:
    EARLY_WINDOW_S = int(float(os.getenv("EARLY_WINDOW_MIN", "10")) * 60)

# Liquidity crush:
_LIQ_FRAC = os.getenv("KILL_LIQ_FRACTION")
LIQ_CRUSH_FRAC = float(_LIQ_FRAC) if _LIQ_FRAC is not None else 0.0  # 0 ⇒ desactivado si no hay entry_liq
LIQ_CRUSH_DROP_PCT = float(os.getenv("LIQ_CRUSH_DROP_PCT", "0"))     # alternativa si no usas fracción directa
LIQ_CRUSH_WINDOW_MIN = int(os.getenv("LIQ_CRUSH_WINDOW_MIN", "30"))  # 0 ⇒ sin límite temporal
LIQ_CRUSH_ABS_FRACT = float(os.getenv("LIQ_CRUSH_ABS_FRACT", "0.60"))  # vs CFG.MIN_LIQUIDITY_USD

# ─── Utilidades ─────────────────────────────────────────────────────────────
def _is_solana_address(addr: str) -> bool:
    """Check muy simple: descarta EVM (0x…) y longitudes extrañas."""
    if not addr or addr.startswith("0x"):
        return False
    # Direcciones de mint de Solana suelen estar ~32–44 chars base58.
    return 30 <= len(addr) <= 50


async def _resolve_close_price_usd(
    token_mint: str,
    *,
    price_hint: Optional[float] = None,
    price_source_hint: Optional[str] = None,
) -> Tuple[Optional[float], Optional[str]]:
    """
    Prioridad para snapshot de cierre:
      1) hint del orquestador (si válido)
      2) Jupiter unitario
      3) price_service crítico (saltando caché NIL)
      4) Dex/GT “full” (par), como último recurso
    Devuelve (precio | None, fuente | None)
    """
    # 1) hint del orquestador
    if price_hint is not None and price_hint > 0:
        return float(price_hint), (price_source_hint or "hint")

    # 2) Jupiter unitario
    try:
        jp = await jupiter_price.get_usd_price(token_mint)
        if jp is not None and jp > 0:
            return float(jp), "jupiter"
    except Exception:
        pass

    # 3) price_service crítico
    try:
        ps = await price_service.get_price_usd(token_mint, critical=True)
        if ps is not None and ps > 0:
            return float(ps), "jup_critical"
    except TypeError:
        try:
            ps = await price_service.get_price_usd(token_mint)
            if ps is not None and ps > 0:
                return float(ps), "jup_single"
        except Exception:
            pass
    except Exception:
        pass

    # 4) Dex/GT “full” (por par)
    try:
        tok_full = await price_service.get_price(token_mint, use_gt=True)
        if tok_full and tok_full.get("price_usd"):
            return float(tok_full["price_usd"]), "dex_full"
    except Exception:
        pass

    return None, None


# ─── Precio actual (Dex → Birdeye → GT → native×SOL) ────────────────────────
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

    price = await price_service.get_price_usd(token_addr, use_gt=True, critical=True)
    if price:
        try:
            return float(price)
        except Exception:
            pass

    await asyncio.sleep(2.0)
    price = await price_service.get_price_usd(token_addr, use_gt=True, critical=True)
    if price:
        try:
            return float(price)
        except Exception:
            return 0.0

    return 0.0


# ─── Ejecución preferente Jupiter (si hay router) ───────────────────────────
async def _sell_execute_prefer_jupiter(
    token_addr: str,
    qty_lamports: int,
    *,
    token_mint: Optional[str],
    liquidity_usd: Optional[float],
) -> Tuple[bool, Dict[str, object]]:
    """
    Intenta vender priorizando Jupiter (si hay router). Fallback a gmgn.sell
    solo si la liquidez es suficiente. Devuelve (ok, payload_dict).
    """
    # 1) Jupiter si está disponible
    if _JUP_ROUTER_AVAILABLE and jupiter is not None:
        try:
            # Nota: algunos routers esperan cantidad en *tokens*, no lamports del token.
            # Aquí asumimos que el router sabe interpretar qty_lamports (SDK propio).
            quote = await jupiter.get_quote(input_mint=token_mint or token_addr,
                                            output_mint=SOL_MINT,
                                            amount_tokens=qty_lamports)
            if getattr(quote, "ok", False):
                try:
                    txid = await jupiter.execute_swap(quote)
                    return True, {"signature": txid, "route": {"jup_quote": True}}
                except Exception as exc:
                    log.warning("[seller] Jupiter execute_swap falló: %s", exc)
            else:
                log.info("[seller] Jupiter no ofrece ruta válida para cerrar %s", (token_mint or token_addr)[:6])
        except Exception as exc:
            log.debug("[seller] Jupiter get_quote error: %s", exc)

    # 2) Fallback a GMGN solo si hay liquidez decente
    try:
        if liquidity_usd is not None and liquidity_usd < float(CFG.MIN_LIQUIDITY_USD):
            log.info("[seller] Low liquidity (%.0f < %.0f) y sin ruta Jupiter → skip/queue",
                     liquidity_usd, float(CFG.MIN_LIQUIDITY_USD))
            return False, {
                "signature": "SKIP_LOW_LIQ",
                "route": {},
                "ok": False,
                "price_used_usd": None,
                "price_source_close": None,
            }

        resp = await gmgn.sell(token_addr, qty_lamports)
        return True, {"signature": resp.get("signature"), "route": resp.get("route", {})}
    except Exception as e:
        log.exception("[seller] Fallback gmgn.sell error: %s", e)
        return False, {
            "signature": "ERROR",
            "route": {},
            "ok": False,
            "error": str(e),
            "price_used_usd": None,
            "price_source_close": None,
        }


# ─── Venta real ─────────────────────────────────────────────────────────────
async def sell(
    token_addr: str,
    qty_lamports: int,
    *,
    token_mint: str | None = None,
    price_hint: float | None = None,
    price_source_hint: str | None = None,
    liquidity_usd: float | None = None,
) -> Dict[str, object]:
    """
    Ejecuta la orden de venta priorizando Jupiter y devuelve datos útiles.

    Retorna
    -------
    dict con:
      - signature: firma de la tx (o código simbólico en error)
      - route: ruta de enrutamiento devuelta por gmgn/Jupiter
      - ok: bool de éxito de envío
      - price_used_usd: float|None precio usado para snapshot/telemetría
      - price_source_close: str|None fuente del precio usado
    """
    key_for_price = token_mint or token_addr

    if not _is_solana_address(token_addr):
        log.error("[seller] Venta bloqueada: address no Solana %r", token_addr)
        return {
            "signature": "INVALID_ADDRESS",
            "route": {},
            "ok": False,
            "price_used_usd": None,
            "price_source_close": None,
        }

    if qty_lamports <= 0:
        log.warning("[seller] Qty=0 — orden ignorada")
        return {
            "signature": "NO_QTY",
            "route": {},
            "ok": False,
            "price_used_usd": None,
            "price_source_close": None,
        }

    # 1) Ejecutar venta (preferentemente Jupiter)
    ok, exec_payload = await _sell_execute_prefer_jupiter(
        token_addr, qty_lamports, token_mint=key_for_price, liquidity_usd=liquidity_usd
    )
    if not ok:
        return exec_payload

    signature = exec_payload.get("signature")
    route = exec_payload.get("route", {})
    ok_flag = True

    # 2) Snapshot de cierre (robusto)
    price_used, src_used = await _resolve_close_price_usd(
        key_for_price, price_hint=price_hint, price_source_hint=price_source_hint
    )

    if price_used is None or price_used <= 0.0:
        try:
            ps = await price_service.get_price_usd(key_for_price, use_gt=True, critical=True)
            if ps and ps > 0:
                price_used, src_used = float(ps), (src_used or "jup_critical")
        except Exception:
            pass

    log.info(
        "[seller] SELL sent sig=%s  price_used=%s  src=%s",
        (signature or "UNKNOWN"),
        f"{price_used:.8g}" if price_used else "None",
        src_used or "None",
    )

    return {
        "signature": signature,
        "route": route,
        "ok": ok_flag,
        "price_used_usd": price_used,
        "price_source_close": src_used,
    }


# ─── Evaluación de condiciones de salida ────────────────────────────────────
def check_exit_conditions(
    position: dict,
    price_now: float,
    tick: Optional[dict] = None,
) -> Optional[str]:
    """
    Devuelve el *motivo* de salida o None si la posición debe permanecer abierta.

    Motivos: "EARLY_DROP", "LIQUIDITY_CRUSH", "TAKE_PROFIT_PARTIAL", "TAKE_PROFIT",
             "STOP_LOSS", "TRAILING_STOP", "NO_EXPANSION", "TIMEOUT"

    Parámetros
    ----------
    position : dict  – debe incluir: buy_price_usd, opened_at, qty_lamports, (...)
    price_now : float – precio actual (USD)
    tick : Optional[dict] – si trae `liquidity_usd`, se evalúa LIQUIDITY_CRUSH
    """
    buy_price  = float(position.get("buy_price_usd", 0.0) or 0.0)
    opened_at  = position.get("opened_at")
    peak_price = float(position.get("peak_price", buy_price) or buy_price)

    if not buy_price or not opened_at:
        return None  # datos insuficientes

    # edad de la posición
    try:
        opened_dt = parse_iso_utc(opened_at) or datetime.now(timezone.utc)
        if opened_dt.tzinfo is None:
            opened_dt = opened_dt.replace(tzinfo=timezone.utc)
        age_sec = (datetime.now(timezone.utc) - opened_dt).total_seconds()
        age_min = age_sec / 60.0
    except Exception:
        age_sec = 0.0
        age_min = 0.0

    # PnL fraccional
    pnl_frac = (price_now - buy_price) / buy_price if buy_price else 0.0

    # actualiza máximo histórico
    if price_now > peak_price:
        position["peak_price"] = price_now
        peak_price = price_now

    # 0) Señales tempranas (prioridad)
    # 0.a) EARLY_DROP (ventana en segundos)
    if EARLY_WINDOW_S > 0 and age_sec <= EARLY_WINDOW_S:
        if pnl_frac <= - (EARLY_DROP_PCT / 100.0):
            return "EARLY_DROP"

    # 0.b) LIQUIDITY_CRUSH (si tick trae liquidity_usd)
    if tick and isinstance(tick, dict):
        curr_liq = tick.get("liquidity_usd")
        try:
            curr_liq_val = float(curr_liq) if curr_liq is not None else None
        except Exception:
            curr_liq_val = None

        window_ok = (LIQ_CRUSH_WINDOW_MIN <= 0) or (age_min <= LIQ_CRUSH_WINDOW_MIN)

        if curr_liq_val and curr_liq_val > 0 and window_ok:
            # intentamos obtener la liquidez de entrada con varias claves posibles
            entry_liq = None
            for k in ("liq_at_buy_usd", "buy_liquidity_usd", "liquidity_at_buy", "entry_liquidity_usd"):
                v = position.get(k)
                if v:
                    try:
                        entry_liq = float(v)
                        break
                    except Exception:
                        entry_liq = None

            # Regla preferente: fracción directa vs entry_liq
            if entry_liq and entry_liq > 0 and LIQ_CRUSH_FRAC > 0:
                if curr_liq_val <= entry_liq * LIQ_CRUSH_FRAC:
                    return "LIQUIDITY_CRUSH"

            # Fallback: caída porcentual vs entry_liq
            if entry_liq and entry_liq > 0 and LIQ_CRUSH_DROP_PCT > 0:
                drop_frac = (entry_liq - curr_liq_val) / entry_liq
                if drop_frac >= (LIQ_CRUSH_DROP_PCT / 100.0):
                    return "LIQUIDITY_CRUSH"

            # Último recurso: corte absoluto vs mínimo global
            if curr_liq_val < float(CFG.MIN_LIQUIDITY_USD) * LIQ_CRUSH_ABS_FRACT:
                return "LIQUIDITY_CRUSH"

    # 1) No-Expansion (a 1h) --------------------------------------------------------
    if age_sec >= 3600 and pnl_frac <= NO_EXPANSION_MAX_FRAC:
        return "NO_EXPANSION"

    # 2) TP parcial / TP total ------------------------------------------------------
    partial_taken = bool(position.get("partial_taken", False))
    if TAKE_PROFIT > 0 and pnl_frac >= TAKE_PROFIT:
        if not partial_taken:
            return "TAKE_PROFIT_PARTIAL"  # primero parcial
        else:
            return "TAKE_PROFIT"          # si ya hubo parcial, cerramos todo

    # 3) SL / Trailing --------------------------------------------------------------
    if STOP_LOSS > 0 and pnl_frac <= -STOP_LOSS:
        return "STOP_LOSS"

    if TRAILING_STOP > 0 and price_now <= peak_price * (1 - TRAILING_STOP):
        return "TRAILING_STOP"

    # 4) Timeout con extensión condicional -----------------------------------------
    # Si el trade va ≥ TRAILING_PCT en verde, permitimos ampliar hasta MAX_HARD_HOLD_H.
    if age_sec >= TIMEOUT_SECONDS:
        if pnl_frac >= TRAILING_STOP and HARD_TIMEOUT_SECONDS > TIMEOUT_SECONDS:
            if age_sec >= HARD_TIMEOUT_SECONDS:
                return "TIMEOUT"
        else:
            return "TIMEOUT"

    return None


# ─── TP Parcial helper ──────────────────────────────────────────────────────
def compute_partial_qty(position: dict, fraction: float) -> int:
    """
    Calcula la cantidad (lamports del token) para una venta parcial.
    Usa `position["qty_lamports"]` o, alternativamente, `position["size_tokens"]`.
    """
    qty_lp = int(position.get("qty_lamports") or 0)
    if qty_lp <= 0:
        # fallback por si la estructura usa otro campo
        qty_lp = int(position.get("size_tokens") or 0)
    take = int(max(1, round(qty_lp * float(fraction))))
    # No tomar más de lo que queda
    take = min(take, qty_lp)
    return max(0, take)


async def apply_partial_tp(
    position: dict,
    *,
    price_hint: Optional[float],
    price_source_hint: Optional[str],
    liquidity_usd: Optional[float],
) -> Optional[Dict[str, object]]:
    """
    Ejecuta la venta parcial (WIN_PCT) y actualiza flags locales de la posición.
    Devuelve el payload de la venta o None si no se pudo ejecutar.
    """
    token_addr = position.get("token_mint") or position.get("token_address") or position.get("address") or ""
    if not token_addr:
        return None

    qty = compute_partial_qty(position, WIN_PCT)
    if qty <= 0:
        log.info("[seller] partial TP sin cantidad disponible")
        return None

    res = await sell(
        token_addr,
        qty,
        token_mint=position.get("token_mint") or token_addr,
        price_hint=price_hint,
        price_source_hint=price_source_hint,
        liquidity_usd=liquidity_usd,
    )
    if res.get("ok"):
        # Actualiza estado local (el caller debe persistir en DB)
        position["partial_taken"] = True
        position["qty_lamports"] = int(position.get("qty_lamports", 0)) - qty
        if position["qty_lamports"] < 0:
            position["qty_lamports"] = 0
        # mantener peak_price (ya se actualiza en loop con price_now)
        log.info("[seller] Partial TP ejecutado: vendidas ~%.0f%% (%d lamports)", WIN_PCT * 100, qty)
        return res
    return None


# ─── Snapshot seguro de cierre (opcional) ───────────────────────────────────
async def safe_close_snapshot(
    position: dict,
    exit_reason: str,
    *,
    price_hint: float | None = None,
    price_source_hint: str | None = None,
) -> dict:
    """
    Construye los campos de cierre con precio de salida *seguro*:
      - Prioriza hint → Jupiter → price_service(critical) → Dex/GT “full”.
      - Fallback al buy_price si sigue faltando precio.
      - Calcula pnl_pct y sella closed_at/exit_reason.
    """
    token_addr = position.get("token_mint") or position.get("token_address") or position.get("address") or ""
    buy_price  = float(position.get("buy_price_usd", 0.0) or 0.0)

    price_now, src_used = await _resolve_close_price_usd(
        token_addr,
        price_hint=price_hint,
        price_source_hint=price_source_hint,
    )

    if price_now is None or price_now <= 0.0:
        # Reintento corto + fallback al buy_price si sigue sin precio
        try:
            ps = await price_service.get_price_usd(token_addr, use_gt=True, critical=True)
            if ps and ps > 0:
                price_now, src_used = float(ps), (src_used or "jup_critical")
            else:
                await asyncio.sleep(2.0)
                ps2 = await price_service.get_price_usd(token_addr, use_gt=True, critical=True)
                if ps2 and ps2 > 0:
                    price_now, src_used = float(ps2), (src_used or "jup_critical")
        except Exception:
            pass

    if price_now is None or price_now <= 0.0:
        if buy_price > 0.0:
            log.warning(
                "[seller] Precio de cierre no disponible para %s. Se usa buy_price como fallback.",
                token_addr[:6],
            )
            price_now = buy_price
            src_used = src_used or "fallback_buy"
        else:
            log.error(
                "[seller] Sin precio de compra ni precio actual para %s. close_price_usd=0.0; pnl_pct=0.0",
                token_addr[:6],
            )
            price_now = 0.0
            src_used = src_used or "none"

    pnl_pct = 0.0 if buy_price <= 0 else ((float(price_now) - buy_price) / buy_price) * 100.0
    closed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return {
        "close_price_usd": float(price_now),
        "pnl_pct": float(pnl_pct),
        "closed_at": closed_at,
        "exit_reason": exit_reason,
        "price_source_close": src_used,
    }


__all__ = [
    "sell",
    "get_current_price",
    "check_exit_conditions",
    "safe_close_snapshot",
    "apply_partial_tp",
    "compute_partial_qty",
]
