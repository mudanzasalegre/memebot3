# memebot3/run_bot.py
"""
⏯️  Orquestador principal del sniper MemeBot 3 (reglas + IA).
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

# ─── módulos internos ───────────────────────────────────────────
from config.config import CFG, BANNED_CREATORS
from config import exits
from db.database import SessionLocal, async_init_db
from db.models import Position, Token, RevivedToken
from fetcher import dexscreener, helius_cluster as clusters, pumpfun, rugcheck, socials
from analytics import filters, insider, trend
from analytics.ai_predict import should_buy, reload_model
from features.builder import build_feature_vector, COLUMNS as FEAT_COLS
from features.store import append as store_append, update_pnl as store_update_pnl
from ml.retrain import retrain_if_better
from utils.descubridor_pares import fetch_candidate_pairs
from utils.lista_pares import (
    agregar_si_nuevo,
    eliminar_par,
    obtener_pares,
    requeue,
    stats as queue_stats,
    retries_left,
)
from utils.data_utils import sanitize_token_data, is_incomplete
from utils.logger import enable_file_logging, warn_if_nulls
from utils.time import utc_now

# ─── CLI / flags ────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="MemeBot 3 – sniper Solana")
parser.add_argument("--dry-run", action="store_true",
                    help="Paper-trading: no envía órdenes on-chain")
parser.add_argument("--log", action="store_true",
                    help="Escribe logs en /logs con rotación horaria")
args = parser.parse_args()
DRY_RUN = args.dry_run or CFG.DRY_RUN

# ─── logging global ─────────────────────────────────────────────
logging.basicConfig(
    level=CFG.LOG_LEVEL,
    format="%(asctime)s  %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
log = logging.getLogger("run_bot")

archived_tokens: dict[str, dict] = {}

# ⇢ buyer / seller según modo
if DRY_RUN:
    from trader import papertrading as buyer   # noqa: E402
    from trader import papertrading as seller  # noqa: E402
    log.info("🔖 DRY-RUN ACTIVADO → usando trader.papertrading")
else:
    from trader import buyer    # noqa: E402
    from trader import seller   # noqa: E402

# logging a fichero opcional
if args.log:
    run_id = enable_file_logging()
    log.info("📂 File-logging activo (run_id %s)", run_id)

# ─── info de esquema ────────────────────────────────────────────
log.info("Schema Parquet cols=%s  •  DB=%s", len(FEAT_COLS), CFG.SQLITE_DB)

# ─── parámetros derivados ───────────────────────────────────────
DISCOVERY_INTERVAL    = CFG.__dict__.get("DISCOVERY_INTERVAL", 45)
SLEEP_SECONDS         = CFG.__dict__.get("SLEEP_SECONDS", 3)
VALIDATION_BATCH_SIZE = CFG.__dict__.get("VALIDATION_BATCH_SIZE", 30)
TRADE_AMOUNT_SOL      = CFG.TRADE_AMOUNT_SOL

TP_PCT        = exits.TAKE_PROFIT_PCT
SL_PCT        = exits.STOP_LOSS_PCT
TRAILING_PCT  = exits.TRAILING_PCT
MAX_HOLDING_H = exits.MAX_HOLDING_H
AI_TH         = CFG.AI_THRESHOLD

# ╭──────────────── BUY PIPELINE (IA + reglas) ─────────────────╮
async def _evaluate_and_buy(token: dict, session: SessionLocal) -> None:
    addr = token["address"]
    # 0) sanea + observabilidad
    token = sanitize_token_data(token)
    warn_if_nulls(token, context=addr[:4])
    log.debug("▶ Eval %s", token.get("symbol", addr[:4]))

    # 1) señales externas (ejecutar solo si hay interés)
    if token.get("discovered_via") == "pumpfun" and not token.get("liquidity_usd") and not token.get("volume_24h_usd"):
        agregar_si_nuevo(addr)
        log.debug("   ↻ pospuesto (esperando liquidez/vol)")
        return
    if token.get("creator") and token.get("creator") in BANNED_CREATORS:
        log.warning("🚫 Creador %s en lista negra, omitiendo %s", token["creator"], addr[:4])
        eliminar_par(addr)
        return

    token["rug_score"]   = await rugcheck.check_token(addr)
    token["cluster_bad"] = await clusters.suspicious_cluster(addr)
    token["social_ok"]   = await socials.has_socials(addr)
    token["trend"]       = await trend.trend_signal(addr)
    token["insider_sig"] = await insider.insider_alert(addr)
    token["score_total"] = filters.total_score(token)

    # 2) liq/vol incompletos  → re-queue + log & exit
    if is_incomplete(token):
        token["is_incomplete"] = 1
        store_append(build_feature_vector(token), 0)
        requeue(addr)
        log.debug("   ↻ requeue (liq/vol = 0)")
        return
    token["is_incomplete"] = 0

    # 3) filtros duros
    if not filters.basic_filters(token):
        vec = build_feature_vector(token)
        # Archivar token si filtros básicos fallaron por liquidez/volumen/holders
        liq = token.get("liquidity_usd", 0.0)
        vol24 = token.get("volume_24h_usd", 0.0)
        holders = token.get("holders", 0)
        too_old = False
        if token.get("created_at"):
            age_days = (utc_now().replace(tzinfo=dt.timezone.utc) - token["created_at"]).days
            if age_days > CFG.MAX_AGE_DAYS:
                too_old = True
        if not too_old and (liq < CFG.MIN_LIQUIDITY_USD or vol24 < CFG.MIN_VOL_USD_24H or holders < CFG.MIN_HOLDERS):
            archived_tokens[addr] = {
                "discovered_at": token.get("created_at", utc_now().replace(tzinfo=dt.timezone.utc)),
                "last_checked": utc_now().replace(tzinfo=dt.timezone.utc),
                "initial_holders": holders,
                "initial_liq": liq,
                "initial_vol": vol24,
            }
            log.info("🕓 %s archivado (liq=%.0f, vol=%.0f, holders=%d) para posible revivir", token.get("symbol", addr[:4]), liq, vol24, holders)
        store_append(vec, 0)
        log.debug("   ✗ filtros básicos")
        eliminar_par(addr)
        return

    # 4) IA
    vec   = build_feature_vector(token)
    proba = should_buy(vec)
    ia_ok = proba >= AI_TH
    store_append(vec, int(ia_ok))

    if not ia_ok:
        log.info("DESCARTADO IA %.2f %% — %s", proba * 100, addr[:4])
        eliminar_par(addr)
        return

    # 5) guarda BD (idempotente)
    try:
        valid_cols = {c.key for c in inspect(Token).mapper.column_attrs}
        await session.merge(Token(**{k: v for k, v in token.items() if k in valid_cols}))
        await session.commit()
    except SQLAlchemyError as e:
        await session.rollback()
        log.warning("DB merge Token: %s", e)

    # 6) compra (o demo)
    if TRADE_AMOUNT_SOL <= 0:
        log.warning("DRY_RUN – no se envía orden real")
        eliminar_par(addr)
        return

    buy_resp  = await buyer.buy(addr, TRADE_AMOUNT_SOL)
    qty       = buy_resp.get("qty_lamports", 0)
    price_usd = buy_resp.get("route", {}).get("quote", {}).get("inAmountUSD")

    pos = Position(
        address=addr,
        symbol=token.get("symbol"),
        qty=qty,
        buy_price_usd=price_usd or 0.0,
        opened_at=utc_now(),
        highest_pnl_pct=0.0,
    )
    try:
        session.add(pos)
        await session.commit()
    except SQLAlchemyError as e:
        await session.rollback()
        log.warning("DB add Position: %s", e)

    log.warning("✔ COMPRADO %s (IA %.1f%%) %s",
                token.get("symbol", "?"), proba * 100, addr)
    eliminar_par(addr)

# ╭──────────────── EXIT STRATEGY ───────────────────────────────╮
async def _load_open_positions(session: SessionLocal) -> Sequence[Position]:
    stmt = select(Position).where(Position.closed.is_(False))
    return (await session.execute(stmt)).scalars().all()

async def _should_exit(pos: Position, price: float, now: dt.datetime) -> bool:
    if not pos.buy_price_usd:
        return False
    pnl = (price - pos.buy_price_usd) / pos.buy_price_usd * 100
    if pnl > pos.highest_pnl_pct:
        pos.highest_pnl_pct = pnl
    return (
        pnl <= pos.highest_pnl_pct - TRAILING_PCT or
        pnl >= TP_PCT or
        pnl <= -SL_PCT or
        (now - pos.opened_at).total_seconds() / 3600 >= MAX_HOLDING_H
    )

async def _check_positions(session: SessionLocal) -> None:
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
        pos.close_price_usd = pair["price_usd"]
        pos.exit_tx_sig     = sell_resp.get("signature")

        pnl_pct = (pos.close_price_usd - pos.buy_price_usd) / pos.buy_price_usd * 100
        store_update_pnl(pos.address, pnl_pct)
        try:
            await session.commit()
        except SQLAlchemyError as e:
            await session.rollback()
            log.warning("DB update Position: %s", e)

        log.warning("💸 VENDIDO %s  pnl=%.1f%%  sig=%s",
                    pos.symbol or pos.address[:4], pnl_pct,
                    (pos.exit_tx_sig or '—')[:6])

# ╭──────────────── RETRAIN LOOP ────────────────────────────────╮
async def retrain_loop() -> None:
    log.info("Retrain-loop activo (domingo %s UTC)", CFG.RETRAIN_HOUR)
    while True:
        now = utc_now()
        if now.weekday() == CFG.RETRAIN_DAY and now.hour == CFG.RETRAIN_HOUR and now.minute < 10:
            try:
                if retrain_if_better():
                    reload_model()
            except Exception as e:  # noqa: BLE001
                log.error("Retrain error: %s", e)
            await asyncio.sleep(3600)
        await asyncio.sleep(300)

# ╭──────────────── MAIN LOOP ───────────────────────────────────╮
async def main_loop() -> None:
    await async_init_db()
    session = SessionLocal()

    last_discovery = 0.0
    log.info(
        "Bot listo  (discover=%ss, lote=%s, pausa=%ss, DRY_RUN=%s, AI_TH=%.2f)",
        DISCOVERY_INTERVAL, VALIDATION_BATCH_SIZE, SLEEP_SECONDS, DRY_RUN, AI_TH,
    )

    while True:
        now = time.monotonic()

        # 1) descubrimiento de pares nuevos
        if now - last_discovery >= DISCOVERY_INTERVAL:
            for addr in await fetch_candidate_pairs():
                agregar_si_nuevo(addr)
            last_discovery = now

        # 2) stream Pump Fun
        for tok in await pumpfun.get_latest_pumpfun():
            try:
                await _evaluate_and_buy(tok, session)
            except Exception as e:  # noqa: BLE001
                log.error("Eval PumpFun %s → %s", tok.get('address', '???')[:4], e)

        # 3) validación de la cola
        for addr in obtener_pares()[:VALIDATION_BATCH_SIZE]:
            try:
                tok = await dexscreener.get_pair(addr)
                if tok:
                    await _evaluate_and_buy(tok, session)
                else:
                    if retries_left(addr) == 1:
                        # Último intento antes de descartar
                        archived_tokens[addr] = {
                            "discovered_at": utc_now().replace(tzinfo=dt.timezone.utc),
                            "last_checked": utc_now().replace(tzinfo=dt.timezone.utc),
                            "initial_holders": 0,
                            "initial_liq": 0.0,
                            "initial_vol": 0.0,
                        }
                        log.info("🕓 %s archivado para reevaluar más tarde (no listado todavía)", addr[:4])
                    requeue(addr)          # re-intenta si aún no indexado
            except Exception as e:
                log.error("get_pair %s → %s", addr[:6], e)

        # 4) posiciones abiertas
        try:
            await _check_positions(session)
        except Exception as e:  # noqa: BLE001
            log.error("Check positions → %s", e)

        # 5) Reevaluación de tokens archivados
        now_utc = utc_now().replace(tzinfo=dt.timezone.utc)
        for addr, info in list(archived_tokens.items()):
            age_min = (now_utc - info["discovered_at"]).total_seconds() / 60.0
            if age_min < 60:
                interval = 180
            elif age_min < 180:
                interval = 300
            else:
                interval = 1800
            if (now_utc - info["last_checked"]).total_seconds() < interval:
                continue
            tok = await dexscreener.get_pair(addr)
            info["last_checked"] = now_utc
            if not tok:
                continue
            liq = tok.get("liquidity_usd", 0.0)
            vol24 = tok.get("volume_24h_usd", 0.0)
            # Por seguridad, interpretamos cambio precio 5m
            pc5 = 0.0
            try:
                pc5 = tok.get("priceChange", {}).get("m5", 0.0) or tok.get("price_change_5m", 0.0)
            except Exception:
                pc5 = 0.0
            pc5_val = float(pc5) if pc5 else 0.0
            if pc5_val < 2:
                pc5_val *= 100.0
            if liq >= CFG.REVIVAL_LIQ_USD and vol24 >= CFG.REVIVAL_VOL1H_USD and pc5_val >= CFG.REVIVAL_PC_5M:
                new_holders = tok.get("holders", 0)
                buyers_delta = new_holders - info.get("initial_holders", 0)
                log.warning("⚡ %s revivido: liq=%.0f$, vol24h=%.0f$ (+%d nuevos holders) – re-evaluando", tok.get("symbol", addr[:4]), liq, vol24, buyers_delta if buyers_delta >= 0 else 0)
                try:
                    session.add(RevivedToken(token_address=addr, first_listed=tok.get("created_at") or info["discovered_at"], revived_at=now_utc, liq_revived=liq, vol_revived=vol24, buyers_delta=buyers_delta if buyers_delta >= 0 else 0))
                    await session.commit()
                except SQLAlchemyError as e:
                    await session.rollback()
                    log.warning("DB insert RevivedToken: %s", e)
                archived_tokens.pop(addr, None)
                try:
                    await _evaluate_and_buy(tok, session)
                except Exception as e:
                    log.error("Eval revival %s → %s", addr[:4], e)

        # 6) métricas de observabilidad
        pend, requeued = queue_stats()
        log.debug("Pendientes=%d  Requeued=%d", pend, requeued)

        await asyncio.sleep(SLEEP_SECONDS)

# ╭────────────────── ENTRY POINT ───────────────────────────────╮
async def _runner() -> None:
    await asyncio.gather(main_loop(), retrain_loop())

if __name__ == "__main__":
    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        log.info("⏹️  Bot detenido por usuario")
