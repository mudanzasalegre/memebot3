# memebot3/run_bot.py
"""
⏯️  Orquestador principal del sniper MemeBot 3
──────────────────────────────────────────────
Cambios 2025-07-27
• FIX logger: _log_token ya no lanza TypeError cuando age/liquidity/etc = None/NaN
• DRY-RUN compra siempre 0.01 SOL
• Fix TZ en _should_exit()
• Cierre de emergencia cuando no hay PNL
• Embudo métricas vía utils.logger.log_funnel()  (cada 60 s)
• Filtro duro tolerante  (None ⇒ requeue 60 s)
• Labeler interno cada hora
• Deduplicación de posiciones
• Log de cola pending/requeued/cooldown
• Market-cap min/max desde .env
• ⭐  liq/vol/mcap = NaN  ⇒ rama “incomplete”
"""

from __future__ import annotations

# ——— stdlib ————————————————————————————————————————
import argparse
import asyncio
import datetime as dt
import logging
import math
import time
from typing import Sequence

# ——— Logger formatter seguro ———————————————————
def _fmt(v, pattern="{:.1f}"):
    if v is None:
        return "?"
    if isinstance(v, float):
        try:
            if math.isnan(v):
                return "?"
        except Exception:
            pass
    try:
        return pattern.format(v)
    except Exception:
        return str(v)

# Reduce ruido
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

# ——— SQLAlchemy async ——————————————————————————————
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.inspection import inspect

# ——— Configuración y salidas ——————————————————
from config.config import (
    CFG,
    BANNED_CREATORS,
    MIN_MARKET_CAP_USD,
    MAX_MARKET_CAP_USD,
)
from config import exits

# ——— DB y modelos ——————————————————————————————
from db.database import SessionLocal, async_init_db
from db.models import Position, Token

# ——— Análisis y señales ——————————————————————
from fetcher import dexscreener, helius_cluster as clusters, pumpfun, rugcheck, socials
from analytics import filters, insider, trend, requeue_policy
from analytics.ai_predict import should_buy, reload_model

# ——— Features / ML —————————————————————————————
from features.builder import build_feature_vector
from features.store import append as store_append, update_pnl as store_update_pnl, export_csv as store_export_csv
from ml.retrain import retrain_if_better

# ——— Utils / Gestión pares ——————————————————————
from utils.descubridor_pares import fetch_candidate_pairs
from utils import lista_pares
from utils.lista_pares import agregar_si_nuevo, eliminar_par, obtener_pares, requeue, stats as queue_stats
from utils.data_utils import sanitize_token_data, apply_default_values
from utils.logger import enable_file_logging, warn_if_nulls, log_funnel
from utils.solana_rpc import get_sol_balance
from utils.time import utc_now
from labeler.win_labeler import label_positions

# ╭──────────── CLI ─────────────────────────╮
parser = argparse.ArgumentParser(description="MemeBot 3 – sniper Solana")
parser.add_argument("--dry-run", action="store_true", help="Paper-trading")
parser.add_argument("--log",     action="store_true", help="Gira logs en /logs")
args    = parser.parse_args()
DRY_RUN = args.dry_run or CFG.DRY_RUN

# ╭──────────── Logging y módulos trader ─────╮
logging.basicConfig(
    level=CFG.LOG_LEVEL,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
log = logging.getLogger("run_bot")

if DRY_RUN:
    from trader import papertrading as buyer
    from trader import papertrading as seller
    log.info("🔖 DRY-RUN ACTIVADO – trader.papertrading")
else:
    from trader import buyer, seller

if args.log:
    run_id = enable_file_logging()
    log.info("📂 File-logging activo (run_id %s)", run_id)

# ╭──────────── Constantes CFG ───────────────╮
DISCOVERY_INTERVAL = CFG.DISCOVERY_INTERVAL
SLEEP_SECONDS = CFG.SLEEP_SECONDS
VALIDATION_BATCH_SIZE = CFG.VALIDATION_BATCH_SIZE
TRADE_AMOUNT_SOL_CFG = CFG.TRADE_AMOUNT_SOL
GAS_RESERVE_SOL = CFG.GAS_RESERVE_SOL
MIN_SOL_BALANCE = CFG.MIN_SOL_BALANCE
WALLET_POLL_INTERVAL = 30

TP_PCT = exits.TAKE_PROFIT_PCT
SL_PCT = exits.STOP_LOSS_PCT
TRAILING_PCT = exits.TRAILING_PCT
MAX_HOLDING_H = exits.MAX_HOLDING_H
AI_TH = CFG.AI_THRESHOLD

_wallet_sol_balance = 0.0
_last_wallet_check = 0.0

_stats = {
    "raw_discovered": 0,
    "incomplete": 0,
    "filtered_out": 0,
    "ai_pass": 0,
    "bought": 0,
    "sold": 0,
    "requeues": 0,
    "requeue_success": 0,
}
_last_stats_print = time.monotonic()
_last_csv_export = time.monotonic()

# ╭──────────── Balance wallet ───────────────╮
async def _refresh_balance(now_mono: float) -> None:
    global _wallet_sol_balance, _last_wallet_check
    if now_mono - _last_wallet_check < WALLET_POLL_INTERVAL:
        return
    try:
        _wallet_sol_balance = await get_sol_balance()
        _last_wallet_check = now_mono
        log.debug("💰 Wallet = %.3f SOL", _wallet_sol_balance)
    except Exception as e:
        log.warning("get_sol_balance → %s", e)

def _compute_trade_amount() -> float:
    if DRY_RUN:
        return 0.01
    usable = max(0.0, _wallet_sol_balance - GAS_RESERVE_SOL)
    if usable < MIN_SOL_BALANCE:
        return 0.0
    return min(TRADE_AMOUNT_SOL_CFG, usable)

# ╭──────────── Labeler ──────────────────────╮
async def _periodic_labeler() -> None:
    while True:
        try:
            await label_positions()
        except Exception as e:
            log.error("label_positions → %s", e)
        await asyncio.sleep(3600)

# ╭──────────── Evaluación compra ─────────────╮
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

# ──────────────────────────────────────────────────
async def _evaluate_and_buy(token: dict, session: SessionLocal) -> None:
    """Evalúa un token y ejecuta compra si procede."""
    global _wallet_sol_balance

    addr = token["address"]
    _stats["raw_discovered"] += 1

    # — sanitize + warning campos críticos nulos —
    token = sanitize_token_data(token)
    warn_if_nulls(token, context=addr[:4])
    _log_token(token, addr)              # ← helper DEBUG

    # — deduplicación: posición abierta —
    exists = await session.scalar(
        select(Position).where(Position.address == addr, Position.closed.is_(False))
    )
    if exists:
        eliminar_par(addr)
        return

    # — descartes rápidos —
    if token.get("creator") in BANNED_CREATORS:
        eliminar_par(addr)
        return
    if token.get("discovered_via") == "pumpfun" and not token.get("liquidity_usd"):
        requeue(addr, reason="no_liq")
        _stats["requeues"] += 1
        return

    # — incompleto ▸ **solo sin liquidez** —
    if token.get("liquidity_usd") in (None, 0) or (
        isinstance(token.get("liquidity_usd"), float)
        and math.isnan(token["liquidity_usd"])
    ):
        _stats["incomplete"] += 1
        token["is_incomplete"] = 1
        store_append(build_feature_vector(token), 0)

        meta     = lista_pares.meta(addr) or {}
        attempts = int(meta.get("attempts", 0))
        backoff  = [60, 180, 420][min(attempts, 2)]

        log.info(
            "↩️  Reencolando %s (no_liq, intento %s)",
            token.get("symbol") or addr[:4],
            attempts + 1,
        )

        if attempts >= 3:
            eliminar_par(addr)
        else:
            requeue(addr, reason="no_liq", backoff=backoff)
            _stats["requeues"] += 1
        return

    # completar métricas opcionales con defaults
    token = apply_default_values(token)
    token["is_incomplete"] = 0

    # Si venía re-enqueue y ahora ya está completo → log
    if lista_pares.retries_left(addr) < lista_pares.MAX_RETRIES:
        log.debug(
            "✔ %s completado · liq=%.0f vol24h=%.0f mcap=%.0f",
            addr[:4],
            token["liquidity_usd"],
            token["volume_24h_usd"],
            token["market_cap_usd"],
        )

    # — señales baratas —
    token["social_ok"] = await socials.has_socials(addr)

    # ★—— TREND tolerante (sin requeue por 404) —★
    try:
        token["trend"], token["trend_fallback_used"] = await trend.trend_signal(addr)
    except trend.Trend404Retry:
        # No hay datos de tendencia aún → seguimos con trend neutro
        log.debug("⚠️  %s sin datos de tendencia – continúa sin trend", addr[:4])
        token["trend"] = 0.0
        token["trend_fallback_used"] = True

    # — resto de señales —
    token["insider_sig"] = await insider.insider_alert(addr)
    token["score_total"] = filters.total_score(token)

    # — filtro duro tolerante —
    res = filters.basic_filters(token)
    if res is not True:
        meta     = lista_pares.meta(addr) or {}
        attempts = int(meta.get("attempts", 0))
        keep, delay, reason = requeue_policy.decide(
            token, attempts, meta.get("first_seen", time.time())
        )
        if keep:
            requeue(addr, reason=reason, backoff=delay)
            _stats["requeues"] += 1
        else:
            _stats["filtered_out"] += 1
            store_append(build_feature_vector(token), 0)
            eliminar_par(addr)
        return

    # — señales “caras” —
    token["rug_score"]   = await rugcheck.check_token(addr)
    token["cluster_bad"] = await clusters.suspicious_cluster(addr)
    token["score_total"] = filters.total_score(token)

    # — IA —
    vec   = build_feature_vector(token)
    proba = should_buy(vec)
    if proba < AI_TH:
        _stats["filtered_out"] += 1
        store_append(vec, 0)
        eliminar_par(addr)
        return

    _stats["ai_pass"] += 1
    store_append(vec, 1)

    # — balance —
    amount_sol = _compute_trade_amount()
    if amount_sol < MIN_SOL_BALANCE:
        eliminar_par(addr)
        return

    # — persistir Token —
    valid_cols = {c.key for c in inspect(Token).mapper.column_attrs}
    await session.merge(Token(**{k: v for k, v in token.items() if k in valid_cols}))
    await session.commit()

    # — BUY —
    try:
        buy_resp = await buyer.buy(addr, amount_sol)
    except Exception:
        eliminar_par(addr)
        return

    qty_lp    = buy_resp.get("qty_lamports", 0)
    price_usd = (
        buy_resp.get("route", {}).get("quote", {}).get("inAmountUSD")
        or token.get("price_usd")
        or 0.0
    )

    if not DRY_RUN:
        _wallet_sol_balance = max(_wallet_sol_balance - amount_sol, 0.0)

    pos = Position(
        address=addr,
        symbol=token.get("symbol"),
        qty=qty_lp,
        buy_price_usd=price_usd,
        opened_at=utc_now(),
        highest_pnl_pct=0.0,
    )
    session.add(pos)
    await session.commit()

    if (lista_pares.meta(addr) or {}).get("attempts", 0) > 0:
        _stats["requeue_success"] += 1
    _stats["bought"] += 1
    eliminar_par(addr)

# ╭──────────── EXIT STRATEGY ──────────────────────╮
async def _load_open_positions(ses: SessionLocal) -> Sequence[Position]:
    stmt = select(Position).where(Position.closed.is_(False))
    return (await ses.execute(stmt)).scalars().all()


async def _should_exit(pos: Position, price: float, now: dt.datetime) -> bool:
    opened = pos.opened_at
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=dt.timezone.utc)

    pnl = None
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


async def _check_positions(session: SessionLocal) -> None:
    global _wallet_sol_balance
    for pos in await _load_open_positions(session):
        pair = await dexscreener.get_pair(pos.address)
        if not pair or not pair.get("price_usd"):
            continue

        now = utc_now()
        if not await _should_exit(pos, pair["price_usd"], now):
            continue

        sell_resp = await seller.sell(pos.address, pos.qty)
        pos.closed          = True
        pos.closed_at       = now
        pos.close_price_usd = pair.get("price_usd")
        pos.exit_tx_sig     = sell_resp.get("signature")

        pnl_pct = (
            None
            if pos.close_price_usd is None or pos.buy_price_usd is None
            else (pos.close_price_usd - pos.buy_price_usd) / pos.buy_price_usd * 100
        )
        store_update_pnl(pos.address, pnl_pct if pnl_pct is not None else -100.0)
        _stats["sold"] += 1

        try:
            await session.commit()
        except SQLAlchemyError:
            await session.rollback()

        if not DRY_RUN:
            try:
                _wallet_sol_balance += pos.qty / 1e9
            except Exception:
                pass

# ╭──────────── Loop de entrenamiento ─────────╮
async def retrain_loop() -> None:
    import calendar
    wd = calendar.day_name[CFG.RETRAIN_DAY]
    log.info("Retrain-loop activo (%s %s UTC)", wd, CFG.RETRAIN_HOUR)
    while True:
        now = utc_now()
        if (
            now.weekday() == CFG.RETRAIN_DAY
            and now.hour == CFG.RETRAIN_HOUR
            and now.minute < 10
        ):
            try:
                if retrain_if_better():
                    reload_model()
            except Exception as e:
                log.error("Retrain error: %s", e)
            await asyncio.sleep(3600)
        await asyncio.sleep(300)

# ╭──────────── MAIN LOOP ─────────────────────╮
async def main_loop() -> None:
    session = SessionLocal()
    last_discovery = 0.0
    log.info("Ready (discover=%ss, batch=%s, sleep=%ss, DRY_RUN=%s, AI_TH=%.2f)",
             DISCOVERY_INTERVAL, VALIDATION_BATCH_SIZE, SLEEP_SECONDS, DRY_RUN, AI_TH)

    global _wallet_sol_balance, _last_stats_print, _last_csv_export
    _wallet_sol_balance = await get_sol_balance()
    log.info("Balance inicial: %.3f SOL", _wallet_sol_balance)

    while True:
        now_mono = time.monotonic()
        await _refresh_balance(now_mono)

        # 1) descubrimiento nuevos pares
        if now_mono - last_discovery >= DISCOVERY_INTERVAL:
            for addr in await fetch_candidate_pairs():
                agregar_si_nuevo(addr)
            last_discovery = now_mono

        # 2) stream Pump Fun
        for tok in await pumpfun.get_latest_pumpfun():
            try:
                await _evaluate_and_buy(tok, session)
            except Exception as e:
                log.error("Eval PumpFun %s → %s", tok.get("address", "???")[:4], e)

        # 3) validación cola
        for addr in obtener_pares()[:VALIDATION_BATCH_SIZE]:
            try:
                tok = await dexscreener.get_pair(addr)
                if tok:
                    await _evaluate_and_buy(tok, session)
                else:
                    requeue(addr, reason="dex_nil")
                    _stats["requeues"] += 1
            except Exception as e:
                log.error("get_pair %s → %s", addr[:6], e)

        # 4) posiciones abiertas
        try:
            await _check_positions(session)
        except Exception as e:
            log.error("Check positions → %s", e)

        # 5) métricas embudo + cola
        now_mono = time.monotonic()
        if now_mono - _last_stats_print >= 60:
            log_funnel(_stats)
            pend, req, cool = queue_stats()
            log.info(
                "Queue %d pending (%d requeued, %d cooldown) requeues=%d succ=%d",
                pend,
                req,
                cool,
                _stats["requeues"],
                _stats["requeue_success"],
            )
            if _stats["raw_discovered"] and (
                _stats["incomplete"] / _stats["raw_discovered"] > 0.5
            ):
                log.warning(
                    "⚠️  Ratio incomplete alto: %.1f%%",
                    _stats["incomplete"] / _stats["raw_discovered"] * 100,
                )
            _last_stats_print = now_mono

        if now_mono - _last_csv_export >= 3600:
            store_export_csv()
            _last_csv_export = now_mono

        await asyncio.sleep(SLEEP_SECONDS)

# ╭──────────── Entrypoint ─────────────────────╮
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