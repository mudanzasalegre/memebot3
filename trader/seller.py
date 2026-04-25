# trader/seller.py
"""
Interfaz unificada para salidas en modo REAL:
    • Enviar la orden real de venta priorizando Jupiter (si hay router);
      fallback a gmgn.sell solo con liquidez suficiente (o si no se puede determinar).
    • Evaluar condiciones de salida (TP/SL/Trailing/Timeout/No-Expansion + señales extra)
    • Obtener precio de cierre de forma robusta (hint → Jupiter → critical → Dex/GT)
    • Snapshot coherente incluso si no hay precio (fallback = buy_price)

Notas importantes (alineación con tu orquestador):
─────────────────────────────────────────────────
- El “TP parcial real” lo debe orquestar run_bot.py (DB + flags + trailing + reason).
  Aquí dejamos helpers por compatibilidad (apply_partial_tp), pero seller.py NO decide
  por sí solo cuándo hacer el parcial en real trading (eso es del monitor).
- Para vender vía Jupiter de verdad, se usa:
      jupiter_router.get_quote(..., amount_lamports=...)
      jupiter_router.execute_swap(quote=quote.raw | QuoteResult)
  (y si falla, fallback a GMGN bajo condición de liquidez).

2026-01 (parche):
────────────────
- FIX: jupiter.get_quote() debe recibir amount_lamports (NO amount_tokens).
- FIX: execute_swap() recibe quote raw (dict) o QuoteResult (según implementación).
- Mejoras: fallback liquidity check más robusto (intenta resolver liquidity si viene None).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from config.config import CFG
import analytics.exit_policy as exit_policy
from utils.time import parse_iso_utc
from utils import price_service
from fetcher import jupiter_price

# Router Jupiter opcional (quote + swap real)
try:
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
TAKE_PROFIT_PCT = float(getattr(CFG, "TAKE_PROFIT_PCT", 0.0) or 0.0)
STOP_LOSS_PCT = float(getattr(CFG, "STOP_LOSS_PCT", 0.0) or 0.0)
TRAILING_PCT = float(getattr(CFG, "TRAILING_PCT", 0.0) or getattr(CFG, "TRAILING_PCT", 0.0) or 0.0)
MAX_HOLDING_H = float(getattr(CFG, "MAX_HOLDING_H", 24) or 24)

TAKE_PROFIT = TAKE_PROFIT_PCT / 100.0
STOP_LOSS = abs(STOP_LOSS_PCT) / 100.0
TRAILING_STOP = (float(getattr(CFG, "TRAILING_PCT", TRAILING_PCT) or TRAILING_PCT) / 100.0) if TRAILING_PCT else 0.0
TIMEOUT_SECONDS = int(MAX_HOLDING_H * 3600)

# TP parcial (fracción de la posición a realizar) — compat
# (run_bot lo orquesta; aquí solo para helper apply_partial_tp)
try:
    _partial_cfg = getattr(CFG, "TP_PARTIAL_FRACTION", None)
    if _partial_cfg is None:
        _partial_cfg = getattr(CFG, "PARTIAL_TP_FRACTION", 0.30)
    PARTIAL_TP_FRACTION = float(_partial_cfg)
except Exception:
    PARTIAL_TP_FRACTION = 0.30
PARTIAL_TP_FRACTION = min(max(PARTIAL_TP_FRACTION, 0.05), 0.95)  # clamp 5%..95%

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
try:
    EARLY_DROP_PCT = float(_EARLY_DROP_PCT)
except Exception:
    EARLY_DROP_PCT = 45.0

# Ventana early-drop en segundos (o fallback minutos)
if (ew_s := os.getenv("KILL_EARLY_WINDOW_S")) is not None:
    try:
        EARLY_WINDOW_S = int(ew_s)
    except Exception:
        EARLY_WINDOW_S = 0
else:
    try:
        EARLY_WINDOW_S = int(float(os.getenv("EARLY_WINDOW_MIN", "10")) * 60)
    except Exception:
        EARLY_WINDOW_S = 0

# Liquidity crush:
_LIQ_FRAC = os.getenv("KILL_LIQ_FRACTION")
try:
    LIQ_CRUSH_FRAC = float(_LIQ_FRAC) if _LIQ_FRAC is not None else 0.0  # 0 ⇒ desactivado si no hay entry_liq
except Exception:
    LIQ_CRUSH_FRAC = 0.0
try:
    LIQ_CRUSH_DROP_PCT = float(os.getenv("LIQ_CRUSH_DROP_PCT", "0"))
except Exception:
    LIQ_CRUSH_DROP_PCT = 0.0
try:
    LIQ_CRUSH_WINDOW_MIN = int(os.getenv("LIQ_CRUSH_WINDOW_MIN", "30"))  # 0 ⇒ sin límite temporal
except Exception:
    LIQ_CRUSH_WINDOW_MIN = 30
try:
    LIQ_CRUSH_ABS_FRACT = float(os.getenv("LIQ_CRUSH_ABS_FRACT", "0.60"))  # vs CFG.MIN_LIQUIDITY_USD
except Exception:
    LIQ_CRUSH_ABS_FRACT = 0.60

# Slippage para swaps de venta (bps). Si no se define, usa el de quote router por defecto.
try:
    JUP_SELL_SLIPPAGE_BPS = int(os.getenv("JUP_SELL_SLIPPAGE_BPS", "150"))  # 1.50% por defecto (meme coins)
except Exception:
    JUP_SELL_SLIPPAGE_BPS = 150


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
      2) Jupiter unitario (jupiter_price)
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

    # 4) Dex/GT “full”
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


async def _resolve_liquidity_usd(token_mint: str) -> Optional[float]:
    """
    Intenta resolver liquidity_usd cuando el caller no la pasa.
    Es un best-effort: no bloquea el sell si no se puede determinar.
    """
    try:
        tok = await price_service.get_price(token_mint, use_gt=True, price_only=True)
        if tok and tok.get("liquidity_usd") is not None:
            try:
                liq = float(tok.get("liquidity_usd") or 0.0)
                return liq if liq > 0 else None
            except Exception:
                return None
    except Exception:
        return None
    return None


# ─── Ejecución preferente Jupiter (si hay router) ───────────────────────────
async def _sell_execute_prefer_jupiter(
    token_addr: str,
    qty_lamports: int,
    *,
    token_mint: str,
    liquidity_usd: Optional[float],
) -> Tuple[bool, Dict[str, object]]:
    """
    Intenta vender priorizando Jupiter (si hay router). Fallback a gmgn.sell
    si la liquidez es suficiente (o no se puede determinar).
    Devuelve (ok, payload_dict).
    """
    # 0) sanity
    if qty_lamports <= 0:
        return False, {"signature": "NO_QTY", "route": {}, "ok": False}

    # 1) Jupiter swap real si está disponible
    if _JUP_ROUTER_AVAILABLE and jupiter is not None:
        try:
            if hasattr(jupiter, "execute_managed_swap") and bool(getattr(jupiter, "JUP_API_KEY", "")):
                managed_resp = await jupiter.execute_managed_swap(
                    input_mint=token_mint,
                    output_mint=SOL_MINT,
                    amount_lamports=int(qty_lamports),
                    slippage_bps=JUP_SELL_SLIPPAGE_BPS,
                )
                route_meta = dict(managed_resp.get("route") or {})
                return True, {
                    "signature": managed_resp.get("signature"),
                    "route": route_meta,
                    "ok": True,
                    "venue": "jupiter_managed",
                }

            # FIX: Jupiter quote espera amount_lamports (unidades del token input)
            quote = await jupiter.get_quote(
                input_mint=token_mint,
                output_mint=SOL_MINT,
                amount_lamports=int(qty_lamports),
                slippage_bps=JUP_SELL_SLIPPAGE_BPS,
                only_direct_routes=False,
            )

            if getattr(quote, "ok", False):
                try:
                    # execute_swap puede aceptar QuoteResult o quote.raw (dict), según implementación
                    try:
                        txid = await jupiter.execute_swap(quote=quote.raw)  # type: ignore[arg-type]
                    except TypeError:
                        # compat: execute_swap(quote_result)
                        txid = await jupiter.execute_swap(quote)  # type: ignore[misc]

                    route_meta = {
                        "router": "jupiter",
                        "priceImpactBps": getattr(quote, "price_impact_bps", None),
                        "inAmount": getattr(quote, "in_amount", None),
                        "outAmount": getattr(quote, "out_amount", None),
                    }
                    return True, {"signature": txid, "route": route_meta, "ok": True, "venue": "jupiter_legacy"}
                except Exception as exc:
                    log.warning("[seller] Jupiter execute_swap falló: %s", exc)
            else:
                log.info(
                    "[seller] Jupiter sin ruta válida para cerrar %s (mint=%s)",
                    token_addr[:6],
                    token_mint[:6],
                )
        except Exception as exc:
            log.debug("[seller] Jupiter get_quote error: %s", exc)

    # 2) Fallback a GMGN solo si hay liquidez decente (si podemos medirla)
    liq = liquidity_usd
    if liq is None:
        liq = await _resolve_liquidity_usd(token_mint)

    try:
        min_liq = float(getattr(CFG, "MIN_LIQUIDITY_USD", 0) or 0)
    except Exception:
        min_liq = 0.0

    if liq is not None and min_liq > 0 and liq < min_liq:
        log.info(
            "[seller] Low liquidity (%.0f < %.0f) y sin ruta Jupiter → skip",
            liq,
            min_liq,
        )
        return False, {
            "signature": "SKIP_LOW_LIQ",
            "route": {"router": "none", "reason": "low_liquidity"},
            "ok": False,
            "price_used_usd": None,
            "price_source_close": None,
        }

    # 3) Ejecuta GMGN
    try:
        resp = await gmgn.sell(token_addr, qty_lamports)
        return True, {
            "signature": resp.get("signature"),
            "route": (resp.get("route", {}) or {"router": "gmgn"}),
            "ok": True,
            "venue": "gmgn",
        }
    except Exception as e:
        log.exception("[seller] Fallback gmgn.sell error: %s", e)
        return False, {
            "signature": "ERROR",
            "route": {"router": "gmgn"},
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
    key_for_quote = token_mint or token_addr

    if not _is_solana_address(token_addr):
        log.error("[seller] Venta bloqueada: address no Solana %r", token_addr)
        return {
            "signature": "INVALID_ADDRESS",
            "route": {},
            "ok": False,
            "price_used_usd": None,
            "price_source_close": None,
        }

    if not _is_solana_address(key_for_quote):
        log.error("[seller] Venta bloqueada: token_mint no Solana %r", key_for_quote)
        return {
            "signature": "INVALID_MINT",
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
        token_addr,
        int(qty_lamports),
        token_mint=key_for_quote,
        liquidity_usd=liquidity_usd,
    )
    if not ok:
        # en caso de "SKIP_LOW_LIQ" o error, devolvemos tal cual
        return exec_payload

    signature = exec_payload.get("signature")
    route = exec_payload.get("route", {}) or {}
    ok_flag = bool(exec_payload.get("ok", True))
    venue = exec_payload.get("venue")

    # 2) Snapshot de cierre (robusto)
    price_used, src_used = await _resolve_close_price_usd(
        key_for_quote, price_hint=price_hint, price_source_hint=price_source_hint
    )

    # Reintento suave si sigue vacío
    if price_used is None or price_used <= 0.0:
        try:
            ps = await price_service.get_price_usd(key_for_quote, use_gt=True, critical=True)
            if ps and ps > 0:
                price_used, src_used = float(ps), (src_used or "jup_critical")
        except Exception:
            pass

    log.info(
        "[seller] SELL sent sig=%s  price_used=%s  src=%s  router=%s",
        (signature or "UNKNOWN"),
        f"{price_used:.8g}" if price_used else "None",
        src_used or "None",
        route.get("router") if isinstance(route, dict) else "unknown",
    )

    return {
        "signature": signature,
        "route": route,
        "ok": ok_flag,
        "price_used_usd": price_used,
        "price_source_close": src_used,
        "venue": venue,
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
    if price_now > float(position.get("peak_price", 0.0) or 0.0):
        position["peak_price"] = float(price_now)

    liq_now = None
    if tick and isinstance(tick, dict):
        try:
            liq_now = float(tick.get("liquidity_usd")) if tick.get("liquidity_usd") is not None else None
        except Exception:
            liq_now = None

    return exit_policy.should_exit(
        position,
        price_now,
        datetime.now(timezone.utc),
        liq_now=liq_now,
    )


# ─── TP Parcial helper ──────────────────────────────────────────────────────
def compute_partial_qty(position: dict, fraction: float) -> int:
    """
    Calcula la cantidad (lamports del token) para una venta parcial.
    Usa `position["qty_lamports"]` o, alternativamente, `position["size_tokens"]`.
    """
    try:
        frac = float(fraction)
    except Exception:
        frac = 0.0
    frac = min(max(frac, 0.0), 1.0)

    qty_lp = int(position.get("qty_lamports") or 0)
    if qty_lp <= 0:
        qty_lp = int(position.get("size_tokens") or 0)

    if qty_lp <= 0 or frac <= 0.0:
        return 0

    take = int(max(1, round(qty_lp * frac)))
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
    Ejecuta la venta parcial (TP_PARTIAL_FRACTION) y actualiza flags locales de la posición.
    Devuelve el payload de la venta o None si no se pudo ejecutar.

    IMPORTANTE: en tu arquitectura, esto debe llamarlo el orquestador (run_bot),
    no seller de forma autónoma.
    """
    token_addr = (
        position.get("token_mint")
        or position.get("token_address")
        or position.get("address")
        or ""
    )
    if not token_addr:
        return None

    qty = compute_partial_qty(position, PARTIAL_TP_FRACTION)
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
        position["partial_taken"] = True
        position["qty_lamports"] = int(position.get("qty_lamports", 0)) - qty
        if position["qty_lamports"] < 0:
            position["qty_lamports"] = 0
        log.info("[seller] Partial TP ejecutado: vendidas ~%.0f%% (%d lamports)", PARTIAL_TP_FRACTION * 100, qty)
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
    token_addr = (
        position.get("token_mint")
        or position.get("token_address")
        or position.get("address")
        or ""
    )
    buy_price = float(position.get("buy_price_usd", 0.0) or 0.0)

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
