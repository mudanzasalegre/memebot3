# memebot3/run_bot.py
"""
⏯️  Orquestador principal del sniper MemeBot 3
──────────────────────────────────────────────
Última revisión · 2025-08-02

Novedades importantes
─────────────────────
1.  Se integra ``utils.price_service.get_price()`` con *fallback*
    GeckoTerminal (GT) —solo se llama a GT en:
       • pares re-encolados (cuando no hubo liquidez/DEX)
       • monitorización de posiciones
2.  La lógica de re-queues distingue «incomplete» rápidos
    (``INCOMPLETE_RETRIES``) de «hard requeues» (``MAX_RETRIES``).
"""

from __future__ import annotations

# ───────── stdlib ────────────────────────────────────────────────────────────
import argparse
import asyncio
import datetime as dt
import logging
import math
import os
import random
import time
from collections import deque
from typing import Sequence

# ----------------------------------------------------------------------------
# Helper de formato “seguro” para logs debug
# ----------------------------------------------------------------------------
def _fmt(val, pattern: str = "{:.1f}") -> str:
    """Convierte números a str de forma robusta (None/NaN → '?')."""
    if val is None:
        return "?"
    if isinstance(val, float) and math.isnan(val):
        return "?"
    try:
        return pattern.format(val)
    except Exception:  # noqa: BLE001
        return str(val)

# Reduce ruido de librerías verbosas
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

# ───────── SQLAlchemy (async) ────────────────────────────────────────────────
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.inspection import inspect

# ───────── Config & exits ───────────────────────────────────────────────────
from config.config import (  # noqa: E402 – after stdlib
    CFG,
    BANNED_CREATORS,
    INCOMPLETE_RETRIES,
)
from config import exits  # take-profit / stop-loss

MIN_MARKET_CAP_USD = CFG.MIN_MARKET_CAP_USD
MAX_MARKET_CAP_USD = CFG.MAX_MARKET_CAP_USD

# ───────── DB & modelos ─────────────────────────────────────────────────────
from db.database import SessionLocal, async_init_db  # noqa: E402
from db.models import Position, Token  # noqa: E402

# ───────── Fetchers / analytics ─────────────────────────────────────────────
from fetcher import dexscreener, helius_cluster as clusters, pumpfun, rugcheck, socials  # noqa: E402
from analytics import filters, insider, trend, requeue_policy  # noqa: E402
from analytics.ai_predict import should_buy, reload_model  # noqa: E402

# ───────── Características + ML store ───────────────────────────────────────
from features.builder import build_feature_vector  # noqa: E402
from features.store import (  # noqa: E402
    append as store_append,
    update_pnl as store_update_pnl,
    export_csv as store_export_csv,
)
from ml.retrain import retrain_if_better  # noqa: E402

# ───────── Utils (queue, precio, etc.) ───────────────────────────────────────
from utils.descubridor_pares import fetch_candidate_pairs  # noqa: E402
from utils import lista_pares, price_service  # ★ precio con fallback GT  # noqa: E402
from utils.lista_pares import (  # noqa: E402
    agregar_si_nuevo,
    eliminar_par,
    obtener_pares,
    requeue,
    stats as queue_stats,
)
from utils.data_utils import sanitize_token_data, apply_default_values  # noqa: E402
from utils.logger import enable_file_logging, warn_if_nulls, log_funnel  # noqa: E402
from utils.solana_rpc import get_sol_balance  # noqa: E402
from utils.time import utc_now  # noqa: E402

# Etiquetado de posiciones ganadoras
from labeler.win_labeler import label_positions  # noqa: E402

# ╭─────────────────────── CLI ───────────────────────────────────────────────╮
parser = argparse.ArgumentParser(description="MemeBot 3 – sniper Solana")
parser.add_argument("--dry-run", action="store_true", help="Paper-trading (sin swaps reales)")
parser.add_argument("--log",     action="store_true", help="Girar logs detallados en /logs")
args = parser.parse_args()

DRY_RUN = args.dry_run or CFG.DRY_RUN

# ╭─────────────────────── Logging básico ────────────────────────────────────╮
logging.basicConfig(
    level=CFG.LOG_LEVEL,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
log = logging.getLogger("run_bot")

if DRY_RUN:
    from trader import papertrading as buyer  # type: ignore
    from trader import papertrading as seller  # type: ignore
    log.info("🔖 DRY-RUN ACTIVADO – trader.papertrading")
else:  # modo real
    from trader import buyer  # type: ignore
    from trader import seller  # type: ignore

if args.log:
    run_id = enable_file_logging()
    log.info("📂 File-logging activo (run_id %s)", run_id)

# ╭─────────────────────── Constantes de configuración ───────────────────────╮
DISCOVERY_INTERVAL     = CFG.DISCOVERY_INTERVAL
SLEEP_SECONDS          = CFG.SLEEP_SECONDS
VALIDATION_BATCH_SIZE  = CFG.VALIDATION_BATCH_SIZE
TRADE_AMOUNT_SOL_CFG   = CFG.TRADE_AMOUNT_SOL
GAS_RESERVE_SOL        = CFG.GAS_RESERVE_SOL
MIN_SOL_BALANCE        = CFG.MIN_SOL_BALANCE
MIN_BUY_SOL            = CFG.MIN_BUY_SOL        # ← nueva línea ⭐
MIN_AGE_MIN            = CFG.MIN_AGE_MIN
WALLET_POLL_INTERVAL   = 30

TP_PCT        = exits.TAKE_PROFIT_PCT
SL_PCT        = exits.STOP_LOSS_PCT
TRAILING_PCT  = exits.TRAILING_PCT
MAX_HOLDING_H = exits.MAX_HOLDING_H
AI_TH         = CFG.AI_THRESHOLD

# ╭─────────────────────── Estado global ─────────────────────────────────────╮
_wallet_sol_balance: float = 0.0
_last_wallet_check   : float = 0.0

_stats = {
    "raw_discovered": 0,
    "incomplete":     0,
    "filtered_out":   0,
    "ai_pass":        0,
    "bought":         0,
    "sold":           0,
    "requeues":       0,
    "requeue_success": 0,
}
_last_stats_print: float = time.monotonic()
_last_csv_export : float = time.monotonic()

# ───────────────────── Cupo/cooldown para Pump.fun quick-price ──────────────
_PF_PRICE_QUOTA        = int(os.getenv("PUMPFUN_PRICE_QUOTA", "4"))       # intentos/ventana
_PF_PRICE_QUOTA_WINDOW = int(os.getenv("PUMPFUN_PRICE_QUOTA_WINDOW", "10"))  # seg
_PF_COOLDOWN_S         = int(os.getenv("PUMPFUN_PRICE_ATTEMPT_COOLDOWN", "25"))
_pf_attempt_bucket: deque[float] = deque(maxlen=64)   # timestamps monotonic
_pf_last_attempt: dict[str, float] = {}

def _pf_can_try_now(addr: str) -> bool:
    """Cuota global y cooldown por token para intentos rápidos de precio (Pump.fun)."""
    now = time.monotonic()

    # cooldown por token
    last = _pf_last_attempt.get(addr, 0.0)
    if now - last < _PF_COOLDOWN_S:
        return False

    # limpia ventana
    while _pf_attempt_bucket and (now - _pf_attempt_bucket[0] > _PFF_PRICE_QUOTA_WINDOW if False else now - _pf_attempt_bucket[0] > _PF_PRICE_QUOTA_WINDOW):
        _pf_attempt_bucket.popleft()

    # cupo global
    if len(_pf_attempt_bucket) >= _PF_PRICE_QUOTA:
        return False

    # reserva hueco
    _pf_attempt_bucket.append(now)
    _pf_last_attempt[addr] = now
    return True

# ╭─────────────────────── Helpers de balance ────────────────────────────────╮
async def _refresh_balance(now_mono: float) -> None:
    """Actualiza el balance de la wallet cada ``WALLET_POLL_INTERVAL`` seg."""
    global _wallet_sol_balance, _last_wallet_check

    if now_mono - _last_wallet_check < WALLET_POLL_INTERVAL:
        return
    try:
        _wallet_sol_balance = await get_sol_balance()
        _last_wallet_check  = now_mono
        log.debug("💰 Wallet = %.3f SOL", _wallet_sol_balance)
    except Exception as exc:  # noqa: BLE001
        log.warning("get_sol_balance → %s", exc)

def _compute_trade_amount() -> float:
    """
    Cuántos SOL destinar a la próxima compra.

    • En DRY_RUN se ignora el balance: siempre usa TRADE_AMOUNT_SOL.
    • En modo real se respeta la reserva de gas y se hace un
      sanity-check para no bajar de MIN_SOL_BALANCE ni de MIN_BUY_SOL.
    """
    # — Paper-trading —
    if DRY_RUN:
        return TRADE_AMOUNT_SOL_CFG        # configurable en .env

    # — Real-trading —
    usable = max(0.0, _wallet_sol_balance - GAS_RESERVE_SOL)

    # si al restar la compra quedaríamos por debajo de los umbrales, abortamos
    if usable < max(MIN_BUY_SOL, MIN_SOL_BALANCE):
        return 0.0

    # gastamos el menor de (importe deseado, saldo disponible)
    return min(TRADE_AMOUNT_SOL_CFG, usable)


# ╭─────────────────────── Labeler periódico ────────────────────────────────╮
async def _periodic_labeler() -> None:
    while True:
        try:
            await label_positions()
        except Exception as exc:
            log.error("label_positions → %s", exc)
        await asyncio.sleep(3600)

# ╭─────────────────────── Logging de nuevos tokens ──────────────────────────╮
def _log_token(tok: dict, addr: str) -> None:
    if not log.isEnabledFor(logging.DEBUG):
        return
    log.debug(
        "⛳ Nuevo %s | liq=%s vol24h=%s mcap=%s age=%s",
        tok.get("symbol") or addr[:4],
        _fmt(tok.get("liquidity_usd"), "{:.0f}"),
        _fmt(tok.get("volume_24h_usd"), "{:.0f}"),
        _fmt(tok.get("market_cap_usd"), "{:.0f}"),
        _fmt(tok.get("age_min"), "{:.1f}m"),
    )

# ╭─────────────────────── Evaluar y comprar ─────────────────────────────────╮
async def _evaluate_and_buy(token: dict, ses: SessionLocal) -> None:
    """Evalúa un token y, si pasa los filtros + IA, lanza la compra."""
    global _wallet_sol_balance

    addr = token["address"]
    _stats["raw_discovered"] += 1

    # 0) — limpieza básica + log preliminar —
    token = sanitize_token_data(token)
    warn_if_nulls(token, context=addr[:4])
    _log_token(token, addr)

    # 1) — duplicado: ya hay posición abierta —
    if await ses.scalar(select(Position).where(Position.address == addr,
                                               Position.closed.is_(False))):
        eliminar_par(addr)
        return

    # 2) — filtros inmediatos —
    if token.get("creator") in BANNED_CREATORS:
        eliminar_par(addr)
        return

    # ★★★ Pump.fun: intento rápido de precio con cuota/cooldown antes de requeue ★★★
    if token.get("discovered_via") == "pumpfun" and not token.get("liquidity_usd"):
        if _pf_can_try_now(addr):
            try:
                tok2 = await price_service.get_price(addr, use_gt=True)
                if tok2 and tok2.get("liquidity_usd"):
                    token.update(tok2)  # ya tenemos liq/vol/mcap/price_usd
                else:
                    requeue(addr, reason="no_liq"); _stats["requeues"] += 1; return
            except Exception:
                requeue(addr, reason="no_liq"); _stats["requeues"] += 1; return
        else:
            requeue(addr, reason="no_liq"); _stats["requeues"] += 1; return

    # 3) — incomplete (sin liquidez) ---------------------------------
    if not token.get("liquidity_usd"):
        # ⇢ solo contamos “incomplete” si el pool ya ha cumplido la edad mínima
        if token.get("age_min", 0.0) >= MIN_AGE_MIN:
            _stats["incomplete"] += 1

        token["is_incomplete"] = 1
        store_append(build_feature_vector(token), 0)

        attempts = int((meta := lista_pares.meta(addr) or {}).get("attempts", 0))
        backoff  = [60, 180, 420][min(attempts, 2)]
        # jitter ±20% para evitar estampidas sincronizadas hacia las APIs
        backoff = int(backoff * random.uniform(0.8, 1.2))
        log.info(
            "↩️  Re-queue %s (no_liq, intento %s)",
            token.get("symbol") or addr[:4],
            attempts + 1,
        )

        if attempts >= INCOMPLETE_RETRIES:
            eliminar_par(addr)
        else:
            requeue(addr, reason="no_liq", backoff=backoff)
            _stats["requeues"] += 1
        return

    # 4) — rellenar defaults y métricas opcionales —
    token = apply_default_values(token)
    token["is_incomplete"] = 0

    # 5) — señales baratas (social, trend, insider…) —
    token["social_ok"] = await socials.has_socials(addr)
    try:
        token["trend"], token["trend_fallback_used"] = await trend.trend_signal(addr)
    except trend.Trend404Retry:
        log.debug("⚠️  %s sin datos trend – continúa", addr[:4])
        token["trend"] = 0.0
        token["trend_fallback_used"] = True

    token["insider_sig"] = await insider.insider_alert(addr)
    token["score_total"] = filters.total_score(token)

    # 6) — filtro duro —
    if filters.basic_filters(token) is not True:
        attempts = int((meta := lista_pares.meta(addr) or {}).get("attempts", 0))
        keep, delay, reason = requeue_policy.decide(token, attempts,
                                                    meta.get("first_seen", time.time()))
        if keep:
            requeue(addr, reason=reason, backoff=delay)
            _stats["requeues"] += 1
        else:
            _stats["filtered_out"] += 1
            store_append(build_feature_vector(token), 0)
            eliminar_par(addr)
        return

    # 7) — señales caras —
    token["rug_score"]   = await rugcheck.check_token(addr)
    token["cluster_bad"] = await clusters.suspicious_cluster(addr)
    token["score_total"] = filters.total_score(token)

    # 8) — IA —
    vec, proba = build_feature_vector(token), should_buy(build_feature_vector(token))
    if proba < AI_TH:
        _stats["filtered_out"] += 1
        store_append(vec, 0)
        eliminar_par(addr)
        return
    _stats["ai_pass"] += 1
    store_append(vec, 1)

    # 9) — cálculo de importe —
    amount_sol = _compute_trade_amount()
    if amount_sol < MIN_SOL_BALANCE:
        eliminar_par(addr)
        return

    # 10) — persistir TOKEN (con NaN→0.0 saneados) —
    try:
        valid_cols = {c.key for c in inspect(Token).mapper.column_attrs}
        await ses.merge(Token(**{k: v for k, v in token.items() if k in valid_cols}))
        await ses.commit()
    except SQLAlchemyError as exc:
        await ses.rollback()
        log.error("DB insert token %s → %s", addr[:4], exc)
        eliminar_par(addr)
        return

    # 11) — BUY —
    try:
        if DRY_RUN:
            buy_resp = await buyer.buy(addr, amount_sol,
                                       price_hint=token.get("price_usd"))
        else:
            buy_resp = await buyer.buy(addr, amount_sol)
    except Exception as exc:
        log.error("buyer.buy %s → %s", addr[:4], exc, exc_info=True)
        eliminar_par(addr)
        return

    qty_lp   = buy_resp.get("qty_lamports", 0)
    price_usd = buy_resp.get("buy_price_usd") or token.get("price_usd") or 0.0

    if not DRY_RUN:
        _wallet_sol_balance = max(_wallet_sol_balance - amount_sol, 0.0)

    ses.add(
        Position(
            address=addr,
            symbol=token.get("symbol"),
            qty=qty_lp,
            buy_price_usd=price_usd,
            opened_at=utc_now(),
            highest_pnl_pct=0.0,
        )
    )
    await ses.commit()

    if (meta := lista_pares.meta(addr)) and meta.get("attempts", 0) > 0:
        _stats["requeue_success"] += 1
    _stats["bought"] += 1
    eliminar_par(addr)

# ╭─────────────────────── Exit strategy ──────────────────────────────────────╮
async def _load_open_positions(ses: SessionLocal) -> Sequence[Position]:
    stmt = select(Position).where(Position.closed.is_(False))
    return (await ses.execute(stmt)).scalars().all()

async def _should_exit(pos: Position, price: float | None, now: dt.datetime) -> bool:
    opened = (
        pos.opened_at.replace(tzinfo=dt.timezone.utc)
        if pos.opened_at.tzinfo is None
        else pos.opened_at
    )

    # ① sin precio → timeout
    if price is None:
        return (now - opened).total_seconds() / 3600 >= MAX_HOLDING_H

    # ② con precio → reglas TP/SL/Trailing
    pnl   = None
    if pos.buy_price_usd:
        pnl = (price - pos.buy_price_usd) / pos.buy_price_usd * 100
        if pnl > pos.highest_pnl_pct:
            pos.highest_pnl_pct = pnl

    return (
        pnl is None
        or pnl <= pos.highest_pnl_pct - TRAILING_PCT
        or pnl >= TP_PCT
        or pnl <= -SL_PCT
        or (now - opened).total_seconds() / 3600 >= MAX_HOLDING_H
    )

async def _check_positions(ses: SessionLocal) -> None:
    """Revisa posiciones abiertas y ejecuta ventas cuando corresponde."""
    global _wallet_sol_balance

    for pos in await _load_open_positions(ses):
        now   = utc_now()

        # ────────────── CAMBIO PRINCIPAL ──────────────
        price = None
        if hasattr(seller, "get_current_price"):
            try:
                price = await seller.get_current_price(pos.address)
            except Exception:
                price = None
        if price is None:
            price = await price_service.get_price_usd(pos.address)  # respaldo

        if not await _should_exit(pos, price, now):
            continue

        # — SELL —
        sell_resp = await seller.sell(pos.address, pos.qty)
        pos.closed          = True
        pos.closed_at       = now
        pos.close_price_usd = price or pos.buy_price_usd or 0.0
        pos.exit_tx_sig     = sell_resp.get("signature")

        # — PnL → store —
        pnl_pct = (
            None
            if pos.close_price_usd is None or pos.buy_price_usd is None
            else (pos.close_price_usd - pos.buy_price_usd) / pos.buy_price_usd * 100
        )
        store_update_pnl(pos.address, pnl_pct if pnl_pct is not None else -100.0)
        _stats["sold"] += 1

        try:
            await ses.commit()
        except SQLAlchemyError:
            await ses.rollback()

        # devolver SOL al balance (real-mode)
        if not DRY_RUN:
            try:
                _wallet_sol_balance += pos.qty / 1e9
            except Exception:  # noqa: BLE001
                pass

# ╭─────────────────────── Loop de entrenamiento ─────────────────────────────╮
async def retrain_loop() -> None:
    import calendar

    weekday = calendar.day_name[CFG.RETRAIN_DAY]
    log.info("Retrain-loop activo (%s %s UTC)", weekday, CFG.RETRAIN_HOUR)

    while True:
        now = utc_now()
        if (
            now.weekday() == CFG.RETRAIN_DAY
            and now.hour   == CFG.RETRAIN_HOUR
            and now.minute < 10
        ):
            try:
                if retrain_if_better():
                    reload_model()
                    log.info("🐢 Retrain completo; modelo recargado en memoria")
            except Exception as exc:
                log.error("Retrain error: %s", exc)
            await asyncio.sleep(3600)
        await asyncio.sleep(300)

# ╭─────────────────────── Main loop ─────────────────────────────────────────╮
async def main_loop() -> None:
    ses             = SessionLocal()
    last_discovery  = 0.0

    log.info(
        "Ready (discover=%ss, batch=%s, sleep=%ss, DRY_RUN=%s, AI_TH=%.2f)",
        DISCOVERY_INTERVAL,
        VALIDATION_BATCH_SIZE,
        SLEEP_SECONDS,
        DRY_RUN,
        AI_TH,
    )

    global _wallet_sol_balance, _last_stats_print, _last_csv_export
    _wallet_sol_balance = await get_sol_balance()
    log.info("Balance inicial: %.3f SOL", _wallet_sol_balance)

    while True:
        now_mono = time.monotonic()
        await _refresh_balance(now_mono)

        # 1) Descubrimiento DexScreener
        if now_mono - last_discovery >= DISCOVERY_INTERVAL:
            for addr in await fetch_candidate_pairs():
                agregar_si_nuevo(addr)
            last_discovery = now_mono

        # 2) Stream Pump Fun
        for tok in await pumpfun.get_latest_pumpfun():
            try:
                await _evaluate_and_buy(tok, ses)
            except Exception as exc:
                log.error("Eval PumpFun %s → %s", tok.get("address", "???")[:4], exc)

        # 3) Validación cola
        for addr in obtener_pares()[:VALIDATION_BATCH_SIZE]:
            try:
                meta    = lista_pares.meta(addr) or {}
                use_gt  = meta.get("attempts", 0) > 0
                tok     = await price_service.get_price(addr, use_gt=use_gt)
                if tok:
                    await _evaluate_and_buy(tok, ses)
                else:
                    requeue(addr, reason="dex_nil")
                    _stats["requeues"] += 1
            except Exception as exc:
                log.error("get_price %s → %s", addr[:6], exc)

        # 4) Posiciones abiertas
        try:
            await _check_positions(ses)
        except Exception as exc:
            log.error("Check positions → %s", exc)

        # 5) Métricas embudo + estado cola
        if (now_mono := time.monotonic()) - _last_stats_print >= 60:
            log_funnel(_stats)
            pend, req, cool = queue_stats()
            log.info(
                "Queue %d pending (%d requeued, %d cooldown) requeues=%d succ=%d",
                pend, req, cool, _stats["requeues"], _stats["requeue_success"],
            )
            if _stats["raw_discovered"] and (
                _stats["incomplete"] / _stats["raw_discovered"] > 0.5
            ):
                log.warning(
                    "⚠️  Ratio incomplete alto: %.1f%%",
                    _stats["incomplete"] / _stats["raw_discovered"] * 100,
                )
            _last_stats_print = now_mono

        # 6) Export CSV cada hora
        if now_mono - _last_csv_export >= 3600:
            store_export_csv()
            _last_csv_export = now_mono

        await asyncio.sleep(SLEEP_SECONDS)

# ╭─────────────────────── Entrypoint ───────────────────────────────────────╮
async def _runner() -> None:
    await async_init_db()
    await asyncio.gather(
        main_loop(),
        retrain_loop(),
        _periodic_labeler(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        log.info("⏹️  Bot detenido por usuario")
