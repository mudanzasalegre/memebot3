# memebot3/run_bot.py
"""
⏯️  Orquestador principal del sniper MemeBot 3
──────────────────────────────────────────────
Cambios 2025-07-20 / 21
• DRY-RUN compra siempre 0.01 SOL
• Fix TZ en _should_exit()
• Cierre de emergencia cuando no hay PNL
• Embudo de métricas utils.logger.log_funnel()  (cada 60 s)
• Filtro duro tolerante (basic_filters → None ⇒ requeue 60 s)
• Runner interno labeler.win_labeler → task cada hora
• 🆕 async_init_db() se llama **una sola vez** en _runner()
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import time
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.inspection import inspect

# ───────── módulos propios ──────────────────────────────────
from config.config import CFG, BANNED_CREATORS
from config import exits
from db.database import SessionLocal, async_init_db
from db.models import Position, Token, RevivedToken
from fetcher import (dexscreener, helius_cluster as clusters, pumpfun,
                     rugcheck, socials)
from analytics import filters, insider, trend
from analytics.ai_predict import should_buy, reload_model
from features.builder import build_feature_vector, COLUMNS as FEAT_COLS
from features.store import append as store_append, update_pnl as store_update_pnl
from ml.retrain import retrain_if_better
from utils.descubridor_pares import fetch_candidate_pairs
from utils.lista_pares import (agregar_si_nuevo, eliminar_par, obtener_pares,
                               requeue, stats as queue_stats)
from utils.data_utils import sanitize_token_data, is_incomplete
from utils.logger import enable_file_logging, warn_if_nulls, log_funnel
from utils.solana_rpc import get_sol_balance
from utils.time import utc_now
from labeler.win_labeler import label_positions

# ╭──────────── CLI / flags ─────────────────────────╮
parser = argparse.ArgumentParser(description="MemeBot 3 – sniper Solana")
parser.add_argument("--dry-run", action="store_true", help="Paper-trading")
parser.add_argument("--log", action="store_true", help="Gira logs en /logs")
args = parser.parse_args()
DRY_RUN = args.dry_run or CFG.DRY_RUN

# ╭──────────── logging root ────────────────────────╮
logging.basicConfig(
    level=CFG.LOG_LEVEL,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
log = logging.getLogger("run_bot")

# ───────── trader segun modo ───────────────────────
if DRY_RUN:
    from trader import papertrading as buyer, papertrading as seller  # noqa: E402
    log.info("🔖 DRY-RUN ACTIVADO – trader.papertrading")
else:
    from trader import buyer, seller                                 # noqa: E402

# file-logging opcional
if args.log:
    run_id = enable_file_logging()
    log.info("📂 File-logging activo (run_id %s)", run_id)

# ───────── constantes de CFG ───────────────────────
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

# ───────── estado runtime ─────────────────────────
_wallet_sol_balance: float = 0.0
_last_wallet_check: float = 0.0

_stats = {
    "raw_discovered": 0,
    "incomplete": 0,
    "filtered_out": 0,
    "ai_pass": 0,
    "bought": 0,
    "sold": 0,
}
_last_stats_print = time.monotonic()

archived_tokens: dict[str, dict] = {}

# ╭──────────── helpers balance ───────────────────╮
async def _refresh_balance(monotonic_now: float) -> None:
    global _wallet_sol_balance, _last_wallet_check
    if monotonic_now - _last_wallet_check < WALLET_POLL_INTERVAL:
        return
    try:
        _wallet_sol_balance = await get_sol_balance()
        _last_wallet_check = monotonic_now
        log.debug("💰 Wallet = %.3f SOL", _wallet_sol_balance)
    except Exception as e:  # noqa: BLE001
        log.warning("get_sol_balance → %s", e)


def _compute_trade_amount() -> float:
    """Dry-run siempre 0.01 SOL; en real respeta reserva de gas."""
    if DRY_RUN:
        return 0.01
    usable = max(0.0, _wallet_sol_balance - GAS_RESERVE_SOL)
    if usable < MIN_SOL_BALANCE:
        return 0.0
    return min(TRADE_AMOUNT_SOL_CFG, usable)

# ╭──────────── labeler background ────────────────╮
async def _periodic_labeler() -> None:
    while True:
        try:
            await label_positions()
        except Exception as e:
            log.error("label_positions → %s", e)
        await asyncio.sleep(3600)

# ╭──────────── BUY PIPELINE ──────────────────────╮
async def _evaluate_and_buy(token: dict, session) -> None:
    global _wallet_sol_balance
    addr = token["address"]
    _stats["raw_discovered"] += 1

    # — sanitize + warning si liq/vol nulos
    token = sanitize_token_data(token)
    warn_if_nulls(token, context=addr[:4])

    # — descartes rápidos
    if token.get("creator") in BANNED_CREATORS:
        eliminar_par(addr)
        return
    if token.get("discovered_via") == "pumpfun" and not token["liquidity_usd"]:
        requeue(addr)
        return

    # — señales baratas
    token["social_ok"] = await socials.has_socials(addr)
    token["trend"] = await trend.trend_signal(addr)
    token["insider_sig"] = await insider.insider_alert(addr)
    token["score_total"] = filters.total_score(token)

    # — incompleto
    if is_incomplete(token):
        _stats["incomplete"] += 1
        token["is_incomplete"] = 1
        store_append(build_feature_vector(token), 0)
        requeue(addr)
        return
    token["is_incomplete"] = 0

    # — filtro duro tolerante
    res = filters.basic_filters(token)
    if res is None:  # liquidez aún 0 → delay
        requeue(addr)
        return
    if res is False:
        _stats["filtered_out"] += 1
        store_append(build_feature_vector(token), 0)
        eliminar_par(addr)
        return

    # — señales caras
    token["rug_score"] = await rugcheck.check_token(addr)
    token["cluster_bad"] = await clusters.suspicious_cluster(addr)
    token["score_total"] = filters.total_score(token)

    # — IA
    vec, proba = build_feature_vector(token), should_buy(token)
    if proba < AI_TH:
        _stats["filtered_out"] += 1
        store_append(vec, 0)
        eliminar_par(addr)
        return
    _stats["ai_pass"] += 1
    store_append(vec, 1)

    # — balance
    amount_sol = _compute_trade_amount()
    if amount_sol < MIN_SOL_BALANCE:
        eliminar_par(addr)
        return

    # — guardar/merge Token
    valid_cols = {c.key for c in inspect(Token).mapper.column_attrs}
    await session.merge(Token(**{k: v for k, v in token.items() if k in valid_cols}))
    await session.commit()

    # — BUY (try / except)
    try:
        buy_resp = await buyer.buy(addr, amount_sol)
    except Exception:
        eliminar_par(addr)
        return

    qty_lp = buy_resp.get("qty_lamports", 0)
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

    _stats["bought"] += 1
    eliminar_par(addr)

# ╭──────────── EXIT STRATEGY ──────────────────────╮
async def _load_open_positions(session) -> Sequence[Position]:
    stmt = select(Position).where(Position.closed.is_(False))
    return (await session.execute(stmt)).scalars().all()


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


async def _check_positions(session) -> None:
    global _wallet_sol_balance
    for pos in await _load_open_positions(session):
        pair = await dexscreener.get_pair(pos.address)
        if not pair or not pair.get("price_usd"):
            continue

        now = utc_now()
        if not await _should_exit(pos, pair["price_usd"], now):
            continue

        sell_resp = await seller.sell(pos.address, pos.qty)
        pos.closed = True
        pos.closed_at = now
        pos.close_price_usd = pair.get("price_usd")
        pos.exit_tx_sig = sell_resp.get("signature")

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

# ╭──────────── retrain loop ──────────────────────╮
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

# ╭──────────── MAIN LOOP ─────────────────────────╮
async def main_loop() -> None:
    session = SessionLocal()  # BD ya inicializada en _runner

    last_discovery = 0.0
    log.info(
        "Ready (discover=%ss, batch=%s, sleep=%ss, DRY_RUN=%s, AI_TH=%.2f)",
        DISCOVERY_INTERVAL,
        VALIDATION_BATCH_SIZE,
        SLEEP_SECONDS,
        DRY_RUN,
        AI_TH,
    )

    global _wallet_sol_balance, _last_stats_print
    _wallet_sol_balance = await get_sol_balance()
    log.info("Balance inicial: %.3f SOL", _wallet_sol_balance)

    while True:
        now_mono = time.monotonic()
        await _refresh_balance(now_mono)

        # 1) descubrimiento de nuevos pares
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

        # 3) validación de la cola
        for addr in obtener_pares()[:VALIDATION_BATCH_SIZE]:
            try:
                tok = await dexscreener.get_pair(addr)
                if tok:
                    await _evaluate_and_buy(tok, session)
                else:
                    requeue(addr)
            except Exception as e:
                log.error("get_pair %s → %s", addr[:6], e)

        # 4) posiciones abiertas
        try:
            await _check_positions(session)
        except Exception as e:
            log.error("Check positions → %s", e)

        # 5) métricas embudo
        if time.monotonic() - _last_stats_print >= 60:
            log_funnel(_stats)
            _last_stats_print = time.monotonic()

        await asyncio.sleep(SLEEP_SECONDS)

# ╭──────────── entry point ───────────────────────╮
async def _runner() -> None:
    # crea tablas (WAL) una sola vez
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
