# memebot3/run_bot.py
"""
⏯️  Orquestador principal del sniper MemeBot 3
──────────────────────────────────────────────
Última revisión · 2025-08-23

Novedades importantes
─────────────────────
1) Ventanas horarias de trading (env):
     TRADING_HOURS=13-16
     TRADING_HOURS_EXTRA=11,18,22
     USE_EXTRA_HOURS=false
   Fuera de ventana → requeue con backoff hasta la siguiente.

2) Compra sólo con precio de Jupiter (solo precio):
     REQUIRE_JUPITER_FOR_BUY=true
   Si Jupiter no devuelve precio → no se compra.

3) Gestión de riesgo (monitor):
   • Early-drop kill: KILL_EARLY_DROP_PCT (def. 45) dentro de
     KILL_EARLY_WINDOW_S (def. 90 s) desde la apertura.
   • Liquidity crush opcional: si dispones de liq_at_buy_usd en la Position
     y el tick trae liquidity_usd, cierra si cae por debajo de
     KILL_LIQ_FRACTION (def. 0.70). Si no hay datos, se omite.

4) Umbral IA dinámico:
   Si existe data/metrics/recommended_threshold.json o el .meta.json del
   modelo trae ai_threshold_recommended, se aplica al arrancar.
"""

from __future__ import annotations

# ───────── stdlib ────────────────────────────────────────────────────────────
import argparse
import asyncio
import datetime as dt
import json
import logging
import math
import os
import random
import time
from collections import deque
from typing import Sequence, Dict, List, Tuple, Optional

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
    USE_JUPITER_PRICE,  # batch Jupiter
)
from config import exits  # take-profit / stop-loss

MIN_MARKET_CAP_USD = CFG.MIN_MARKET_CAP_USD
MAX_MARKET_CAP_USD = CFG.MAX_MARKET_CAP_USD

# ───────── DB & modelos ─────────────────────────────────────────────────────
from db.database import SessionLocal, async_init_db  # noqa: E402
from db.models import Position, Token  # noqa: E402

# ───────── Fetchers / analytics ─────────────────────────────────────────────
from fetcher import (  # noqa: E402
    dexscreener,
    helius_cluster as clusters,
    pumpfun,
    rugcheck,
    socials,
    jupiter_price,  # batch de precios
)
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
from utils import lista_pares, price_service  # precio con fallbacks  # noqa: E402
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
from utils.time import utc_now, parse_iso_utc  # noqa: E402

# Etiquetado de posiciones ganadoras
from labeler.win_labeler import label_positions  # noqa: E402

# ╭─────────────────────── helpers: ventanas horarias ────────────────────────╮
def _parse_hours(spec: str) -> List[Tuple[int, int]]:
    """
    Convierte expresiones tipo "13-16,22,7" a rangos [(13,16),(22,22),(7,7)].
    Soporta rangos cruzando medianoche: "22-2" → [(22,23),(0,2)].
    """
    windows: List[Tuple[int, int]] = []
    if not spec:
        return windows
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            if 0 <= a <= 23 and 0 <= b <= 23:
                if a <= b:
                    windows.append((a, b))
                else:
                    windows.append((a, 23))
                    windows.append((0, b))
        else:
            try:
                h = int(part)
                if 0 <= h <= 23:
                    windows.append((h, h))
            except Exception:
                continue
    # normaliza y ordena
    return sorted(windows)

def _in_windows(now_local: dt.datetime, windows: List[Tuple[int, int]]) -> bool:
    if not windows:
        return True
    h = now_local.hour
    for a, b in windows:
        if a <= h <= b:
            return True
    return False

def _secs_to_next_window(now_local: dt.datetime, windows: List[Tuple[int, int]]) -> int:
    if not windows:
        return 0
    h, m, s = now_local.hour, now_local.minute, now_local.second
    # si ya estamos dentro, siguiente es el inicio del siguiente rango (día actual o siguiente)
    if _in_windows(now_local, windows):
        # encontrar el final del rango actual y sumar hasta el inicio del rango siguiente
        # simplificación: esperamos 15 min para reintentar dentro de la ventana activa
        return 15 * 60
    # buscar el próximo inicio ≥ ahora
    candidates: List[int] = []
    for a, b in windows:
        if a >= h:
            # segundos hasta las a:00 de hoy
            delta = (a - h) * 3600 - m * 60 - s
            candidates.append(delta if delta >= 0 else 0)
        else:
            # hasta mañana a:00
            delta = (24 - h + a) * 3600 - m * 60 - s
            candidates.append(delta)
    return min(candidates) if candidates else 3600

_TRADING_HOURS       = _parse_hours(os.getenv("TRADING_HOURS", ""))
_TRADING_HOURS_EXTRA = _parse_hours(os.getenv("TRADING_HOURS_EXTRA", ""))
_USE_EXTRA_HOURS     = os.getenv("USE_EXTRA_HOURS", "false").lower() == "true"
_REQUIRE_JUP_FOR_BUY = os.getenv("REQUIRE_JUPITER_FOR_BUY", "true").lower() == "true"

def _in_trading_window(now_local: Optional[dt.datetime] = None) -> bool:
    now_local = now_local or dt.datetime.now()
    windows = list(_TRADING_HOURS)
    if _USE_EXTRA_HOURS:
        windows += list(_TRADING_HOURS_EXTRA)
    return _in_windows(now_local, windows)

def _delay_until_window(now_local: Optional[dt.datetime] = None) -> int:
    now_local = now_local or dt.datetime.now()
    windows = list(_TRADING_HOURS)
    if _USE_EXTRA_HOURS:
        windows += list(_TRADING_HOURS_EXTRA)
    return _secs_to_next_window(now_local, windows)

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
MIN_BUY_SOL            = CFG.MIN_BUY_SOL
MIN_AGE_MIN            = CFG.MIN_AGE_MIN
WALLET_POLL_INTERVAL   = 30

TP_PCT        = exits.TAKE_PROFIT_PCT
SL_PCT        = exits.STOP_LOSS_PCT
TRAILING_PCT  = exits.TRAILING_PCT
MAX_HOLDING_H = exits.MAX_HOLDING_H
AI_TH         = CFG.AI_THRESHOLD  # se puede sobre-escribir por tuner

# Kill-switches (riesgo) — valores por defecto razonables
_EARLY_DROP_PCT   = float(os.getenv("KILL_EARLY_DROP_PCT", "45"))
_EARLY_WINDOW_S   = int(os.getenv("KILL_EARLY_WINDOW_S", "90"))
_LIQ_CRUSH_FRAC   = float(os.getenv("KILL_LIQ_FRACTION", "0.70"))  # requiere liq_at_buy_usd + liq tick

# ╭─────────────────────── Carga de AI_THRESHOLD recomendado ─────────────────╮
def _load_ai_threshold_override() -> Optional[float]:
    """
    Intenta leer:
      1) data/metrics/recommended_threshold.json → {"picked": 0.34, ...}
      2) modelo.meta.json → {"ai_threshold_recommended": 0.34, ...}
    """
    # 1) recommended_threshold.json junto a FEATURES_DIR/../metrics
    try:
        metrics_dir = CFG.FEATURES_DIR.parent / "metrics"
        thr_path = metrics_dir / "recommended_threshold.json"
        if thr_path.exists():
            data = json.loads(thr_path.read_text())
            val = data.get("picked")
            if isinstance(val, (int, float)):
                return float(val)
    except Exception:
        pass

    # 2) meta del modelo
    try:
        meta_path = CFG.MODEL_PATH.with_suffix(".meta.json")
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            val = meta.get("ai_threshold_recommended")
            if isinstance(val, (int, float)):
                return float(val)
    except Exception:
        pass
    return None

_thr_override = _load_ai_threshold_override()
if _thr_override is not None:
    old = AI_TH
    AI_TH = float(_thr_override)
    log.info("🎯 AI_THRESHOLD override: %.3f (antes=%.3f)", AI_TH, old)

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
    while _pf_attempt_bucket and (now - _pf_attempt_bucket[0] > _PF_PRICE_QUOTA_WINDOW):
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
        return TRADE_AMOUNT_SOL_CFG

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

# ────────────────────────────────────────────────────────────────────────────
# run_bot.py  — _evaluate_and_buy  (sin cambios de lógica, pero afinado)
# ────────────────────────────────────────────────────────────────────────────
async def _evaluate_and_buy(token: dict, ses: SessionLocal) -> None:
    """Evalúa un token y, si pasa los filtros + IA, lanza la compra."""
    global _wallet_sol_balance

    addr = token["address"]
    _stats["raw_discovered"] += 1

    # 0) — ventana horaria —
    if _TRADING_HOURS or (_USE_EXTRA_HOURS and _TRADING_HOURS_EXTRA):
        if not _in_trading_window():
            delay = max(30, _delay_until_window())
            requeue(addr, reason="off_hours", backoff=delay)
            _stats["requeues"] += 1
            return

    # 1) — limpieza básica + log preliminar —
    token = sanitize_token_data(token)
    warn_if_nulls(token, context=addr[:4])
    _log_token(token, addr)

    # 2) — duplicado: ya hay posición abierta —
    if await ses.scalar(select(Position).where(Position.address == addr,
                                               Position.closed.is_(False))):
        eliminar_par(addr)
        return

    # 3) — filtros inmediatos —
    if token.get("creator") in BANNED_CREATORS:
        eliminar_par(addr)
        return

    # ★ Pump.fun: intento rápido de precio con cuota/cooldown antes de requeue
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

    # 4) — incomplete (sin liquidez) ---------------------------------
    if not token.get("liquidity_usd"):
        # ⇢ solo contamos “incomplete” si el pool ya ha cumplido la edad mínima
        if token.get("age_min", 0.0) >= MIN_AGE_MIN:
            _stats["incomplete"] += 1

        token["is_incomplete"] = 1
        store_append(build_feature_vector(token), 0)

        attempts = int((meta := lista_pares.meta(addr) or {}).get("attempts", 0))
        backoff  = [60, 180, 420][min(attempts, 2)]
        backoff  = int(backoff * random.uniform(0.8, 1.2))  # jitter ±20%
        log.info("↩️  Re-queue %s (no_liq, intento %s)",
                 token.get("symbol") or addr[:4], attempts + 1)

        if attempts >= INCOMPLETE_RETRIES:
            eliminar_par(addr)
        else:
            requeue(addr, reason="no_liq", backoff=backoff)
            _stats["requeues"] += 1
        return

    # 5) — rellenar defaults y métricas opcionales —
    token = apply_default_values(token)
    token["is_incomplete"] = 0

    # 6) — señales baratas (social, trend, insider…) —
    token["social_ok"] = await socials.has_socials(addr)
    try:
        token["trend"], token["trend_fallback_used"] = await trend.trend_signal(addr)
    except trend.Trend404Retry:
        log.debug("⚠️  %s sin datos trend – continúa", addr[:4])
        token["trend"] = 0.0
        token["trend_fallback_used"] = True

    token["insider_sig"] = await insider.insider_alert(addr)
    token["score_total"] = filters.total_score(token)

    # 7) — filtro duro —
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

    # 8) — señales caras —
    token["rug_score"]   = await rugcheck.check_token(addr)
    token["cluster_bad"] = await clusters.suspicious_cluster(addr)
    token["score_total"] = filters.total_score(token)

    # 9) — IA —
    vec = build_feature_vector(token)
    proba = should_buy(vec)
    if proba < AI_TH:
        _stats["filtered_out"] += 1
        store_append(vec, 0)
        eliminar_par(addr)
        return
    _stats["ai_pass"] += 1
    store_append(vec, 1)

    # 10) — importe —
    amount_sol = _compute_trade_amount()
    if amount_sol < MIN_BUY_SOL:
        eliminar_par(addr)
        return

    # 11) — Persistir TOKEN (NaN→0.0 saneados) —
    try:
        valid_cols = {c.key for c in inspect(Token).mapper.column_attrs}
        await ses.merge(Token(**{k: v for k, v in token.items() if k in valid_cols}))
        await ses.commit()
    except SQLAlchemyError as exc:
        await ses.rollback()
        log.error("DB insert token %s → %s", addr[:4], exc)
        eliminar_par(addr)
        return

    # 12) — “Exigir Jupiter” para comprar (solo precio) —
    if _REQUIRE_JUP_FOR_BUY:
        try:
            jtok = await price_service.get_price(addr, price_only=True)  # usa flag interno para bloquear
        except TypeError:
            jtok = None
        except Exception:
            jtok = None
        if not jtok or jtok.get("price_source") != "jupiter":
            log.info("🛑 BUY bloqueado (sin Jupiter price) %s", addr[:6])
            eliminar_par(addr)
            return

    # 13) — BUY —
    try:
        if DRY_RUN:
            buy_resp = await buyer.buy(
                addr, amount_sol,
                price_hint=token.get("price_usd"),
                token_mint=token.get("address") or addr
            )
        else:
            buy_resp = await buyer.buy(addr, amount_sol, token_mint=addr)
    except Exception as exc:
        log.error("buyer.buy %s → %s", addr[:4], exc, exc_info=True)
        eliminar_par(addr)
        return

    qty_lp    = buy_resp.get("qty_lamports", 0)
    price_usd = buy_resp.get("buy_price_usd") or token.get("price_usd") or 0.0
    price_src = buy_resp.get("price_source")

    if not DRY_RUN:
        _wallet_sol_balance = max(_wallet_sol_balance - amount_sol, 0.0)

    # 14) — crear Position (incluye *buy_* métricas y fuente de compra) —
    pos = Position(
        address=addr,
        symbol=token.get("symbol"),
        qty=qty_lp,
        buy_price_usd=price_usd,
        opened_at=utc_now(),
        highest_pnl_pct=0.0,
        buy_liquidity_usd=token.get("liquidity_usd"),
        buy_market_cap_usd=token.get("market_cap_usd"),
        buy_volume_24h_usd=token.get("volume_24h_usd"),
    )
    if hasattr(pos, "token_mint"):
        pos.token_mint = token.get("address") or addr
    if hasattr(pos, "price_source_at_buy"):
        pos.price_source_at_buy = price_src

    # Compat opcional (si existiera el alias en tu modelo)
    if hasattr(pos, "liq_at_buy_usd"):
        try:
            setattr(pos, "liq_at_buy_usd", float(token.get("liquidity_usd") or 0.0))
        except Exception:
            setattr(pos, "liq_at_buy_usd", None)

    ses.add(pos)
    await ses.commit()

    if (meta := lista_pares.meta(addr)) and meta.get("attempts", 0) > 0:
        _stats["requeue_success"] += 1
    _stats["bought"] += 1
    eliminar_par(addr)

# ╭─────────────────────── Exit strategy ──────────────────────────────────────╮
async def _load_open_positions(ses: SessionLocal) -> Sequence[Position]:
    stmt = select(Position).where(Position.closed.is_(False))
    return (await ses.execute(stmt)).scalars().all()

# ────────────────────────────────────────────────────────────────────────────
# run_bot.py  — _should_exit  (usa buy_liquidity_usd en vez de liq_at_buy_usd)
# ────────────────────────────────────────────────────────────────────────────
async def _should_exit(
    pos: Position,
    price: Optional[float],
    now: dt.datetime,
    *,
    liq_now: Optional[float] = None,
) -> bool:
    """
    Devuelve True si debe cerrar:
      • Sin precio → TIMEOUT
      • Con precio → TP / SL / Trailing
      • Early-drop (dentro de _EARLY_WINDOW_S y caída ≥ _EARLY_DROP_PCT)
      • Liquidity crush (opcional; requiere buy_liquidity_usd y liq_now)
    """
    opened = (
        pos.opened_at.replace(tzinfo=dt.timezone.utc)
        if pos.opened_at.tzinfo is None
        else pos.opened_at
    )

    # ① sin precio → timeout
    if price is None:
        return (now - opened).total_seconds() / 3600 >= MAX_HOLDING_H

    # ② early-drop dentro de ventana temprana
    if pos.buy_price_usd and _EARLY_DROP_PCT > 0 and _EARLY_WINDOW_S > 0:
        age_s = (now - opened).total_seconds()
        if age_s <= _EARLY_WINDOW_S:
            try:
                drop_pct = (pos.buy_price_usd - float(price)) / pos.buy_price_usd * 100.0
                if drop_pct >= _EARLY_DROP_PCT:
                    return True
            except Exception:
                pass

    # ③ liquidity crush (si hay datos suficientes)
    try:
        entry_liq = getattr(pos, "buy_liquidity_usd", None)
        if entry_liq and liq_now and _LIQ_CRUSH_FRAC > 0:
            if float(liq_now) <= float(entry_liq) * float(_LIQ_CRUSH_FRAC):
                return True
    except Exception:
        pass

    # ④ TP/SL/Trailing
    pnl = None
    if pos.buy_price_usd:
        pnl = (price - pos.buy_price_usd) / pos.buy_price_usd * 100.0
        if pnl > pos.highest_pnl_pct:
            pos.highest_pnl_pct = pnl

    return (
        pnl is None
        or pnl <= pos.highest_pnl_pct - TRAILING_PCT
        or pnl >= TP_PCT
        or pnl <= -SL_PCT
        or (now - opened).total_seconds() / 3600 >= MAX_HOLDING_H
    )

# ───── precarga de precios en batch para posiciones abiertas ────────────────
async def _prefetch_batch_prices(addrs: List[str]) -> Dict[str, float]:
    """
    Devuelve un dict address->price_usd usando Jupiter Price v3 (Lite).
    Si USE_JUPITER_PRICE=False, devuelve {}.
    """
    if not USE_JUPITER_PRICE or not addrs:
        return {}

    def _looks_like_mint(s: str) -> bool:
        return bool(s) and (not s.startswith("0x")) and (30 <= len(s) <= 50)

    try:
        for m in addrs:
            if not _looks_like_mint(m):
                log.warning("Monitor: ID no parece mint SPL → %r", m)
        prices = await jupiter_price.get_many_usd_prices(addrs)
        log.debug("Jupiter batch: %d/%d precios disponibles", len(prices), len(addrs))
        return prices
    except Exception as exc:
        log.debug("batch jupiter_price → %s", exc)
        return {}
    
# run_bot.py  — _check_positions  (implementada con "liquidity crush" proactivo)
async def _check_positions(ses: SessionLocal) -> None:
    """Revisa posiciones abiertas y ejecuta ventas cuando corresponde."""
    import os

    global _wallet_sol_balance

    positions = await _load_open_positions(ses)
    if not positions:
        return

    # ── límites de sondeo crítico por ciclo ─────────────────────────────
    try:
        _CRIT_MAX = max(int(os.getenv("CRIT_PRICE_MAX_PER_CYCLE", "4")), 0)
    except Exception:
        _CRIT_MAX = 4
    try:
        _CRIT_BOOTSTRAP_MIN = max(int(os.getenv("CRIT_BOOTSTRAP_MIN", "20")), 0)
    except Exception:
        _CRIT_BOOTSTRAP_MIN = 20

    # Umbral de "liquidity crush" (fracción respecto a la de entrada)
    try:
        KILL_LIQ_FRAC = float(os.getenv("KILL_LIQ_FRACTION", "0.70"))
    except Exception:
        KILL_LIQ_FRAC = 0.70

    def _near_exit_zone(pos: Position, now: dt.datetime) -> bool:
        opened_raw = getattr(pos, "opened_at", None)
        if opened_raw is None:
            # Sin fecha: nos quedamos en zona crítica por seguridad (permite crítico)
            return True

        opened: dt.datetime | None = None
        try:
            if isinstance(opened_raw, dt.datetime):
                # Si es naïve, asumimos UTC (mantiene tu comportamiento actual)
                opened = opened_raw if opened_raw.tzinfo else opened_raw.replace(tzinfo=dt.timezone.utc)
            elif isinstance(opened_raw, str):
                opened = parse_iso_utc(opened_raw)
        except Exception:
            opened = None

        if opened is None:
            return True  # No podemos calcular edad → tratamos como “bootstrap”

        age_min = (now - opened).total_seconds() / 60.0
        if age_min <= _CRIT_BOOTSTRAP_MIN:
            return True

        try:
            return (pos.highest_pnl_pct or 0.0) > 0.0
        except Exception:
            return False

    def _buy_was_non_jup(p: Position) -> bool:
        src = getattr(p, "price_source_at_buy", None) or ""
        return src in {"dexscreener", "sol_estimate"}

    # ① Preload batch de precios
    addr_list = [
        (getattr(p, "token_mint", None) or p.address)
        for p in positions
        if (getattr(p, "token_mint", None) or p.address)
    ]
    batch_prices: Dict[str, float] = await _prefetch_batch_prices(addr_list)

    # Métricas por ciclo
    total = len(positions)
    batch_resolved = 0
    fallback_resolved = 0
    critical_resolved = 0
    dex_full_resolved = 0
    no_price = 0
    sells_done = 0
    crit_used = 0

    positions_with_price = 0
    positions_without_price = 0

    consult_source_counts = {"jup_batch": 0, "jup_single": 0, "jup_critical": 0, "dex_full": 0, "none": 0}
    close_source_counts   = {"jup_batch": 0, "jup_single": 0, "jup_critical": 0, "dex_full": 0, "fallback_buy": 0, "none": 0}

    for pos in positions:
        now = utc_now()
        mint_key = getattr(pos, "token_mint", None) or pos.address

        price_src = None
        price: Optional[float] = None
        liq_now: Optional[float] = None
        prefer_dex = _buy_was_non_jup(pos)

        if prefer_dex:
            # 1) Dex/GT (SOLO PRECIO, puede traer liq si está disponible)
            tok_full = None
            try:
                tok_full = await price_service.get_price(mint_key, use_gt=True, price_only=True)
            except Exception:
                tok_full = None
            if tok_full and tok_full.get("price_usd"):
                price = float(tok_full["price_usd"])
                liq_now = tok_full.get("liquidity_usd")
                price_src = "dex_full"
                dex_full_resolved += 1

            # 2) Jupiter batch → single → critical
            if price is None:
                p_b = batch_prices.get(mint_key)
                if p_b is not None:
                    price = p_b
                    price_src = "jup_batch"
                    batch_resolved += 1

            if price is None:
                try:
                    p_s = await price_service.get_price_usd(mint_key)
                except Exception:
                    p_s = None
                if p_s is not None:
                    price = p_s
                    price_src = "jup_single"
                    fallback_resolved += 1

            if price is None and crit_used < _CRIT_MAX and _near_exit_zone(pos, now):
                try:
                    p_c = await price_service.get_price_usd(mint_key, critical=True)
                except TypeError:
                    try:
                        p_c = await price_service.get_price_usd(mint_key)
                    except Exception:
                        p_c = None
                except Exception:
                    p_c = None
                if p_c is not None:
                    price = p_c
                    price_src = "jup_critical"
                    critical_resolved += 1
                crit_used += 1

        else:
            # Camino original: Jupiter → Dex/GT
            price = batch_prices.get(mint_key)
            if price is not None:
                price_src = "jup_batch"
                batch_resolved += 1
            else:
                try:
                    price = await price_service.get_price_usd(mint_key)
                except Exception:
                    price = None
                if price is not None:
                    price_src = "jup_single"
                    fallback_resolved += 1

            if price is None and crit_used < _CRIT_MAX and _near_exit_zone(pos, now):
                try:
                    price = await price_service.get_price_usd(mint_key, critical=True)
                except TypeError:
                    try:
                        price = await price_service.get_price_usd(mint_key)
                    except Exception:
                        price = None
                except Exception:
                    price = None
                if price is not None:
                    price_src = "jup_critical"
                    critical_resolved += 1
                crit_used += 1

            if price is None:
                tok_full = None
                try:
                    tok_full = await price_service.get_price(mint_key, use_gt=True, price_only=True)
                except Exception:
                    tok_full = None
                if tok_full and tok_full.get("price_usd"):
                    price = float(tok_full["price_usd"])
                    liq_now = tok_full.get("liquidity_usd")
                    price_src = "dex_full"
                    dex_full_resolved += 1

        # Métricas de cobertura de precio (consulta)
        if price is None:
            positions_without_price += 1
            no_price += 1
            consult_source_counts["none"] += 1
        else:
            positions_with_price += 1
            consult_source_counts[price_src] = consult_source_counts.get(price_src, 0) + 1  # type: ignore

        # ── Liquidity CRUSH proactivo (si no tenemos liq_now, intenta 1 tick “full”) ──
        if getattr(pos, "buy_liquidity_usd", None) and (liq_now is None):
            try:
                tok_full_liq = await price_service.get_price(mint_key, use_gt=True)  # full: puede traer liquidez
            except Exception:
                tok_full_liq = None
            if tok_full_liq:
                try:
                    liq_now = float(tok_full_liq.get("liquidity_usd") or 0.0)
                except Exception:
                    liq_now = None

        if (
            getattr(pos, "buy_liquidity_usd", None)
            and liq_now
            and KILL_LIQ_FRAC > 0
            and float(liq_now) <= float(pos.buy_liquidity_usd) * float(KILL_LIQ_FRAC)
        ):
            # Venta inmediata por crush de liquidez
            sell_resp = await seller.sell(
                pos.address,
                pos.qty,
                token_mint=mint_key,
                price_hint=price,
                price_source_hint=price_src,
            )
            pos.closed = True
            pos.closed_at = now
            pos.exit_reason = "LIQUIDITY_CRUSH"

            used_close  = (sell_resp or {}).get("price_used_usd")
            used_source = (sell_resp or {}).get("price_source_close")

            if used_close is not None:
                try:
                    pos.close_price_usd = float(used_close)
                except Exception:
                    pos.close_price_usd = price if price is not None else None
            else:
                pos.close_price_usd = price if price is not None else pos.buy_price_usd

            if hasattr(pos, "price_source_at_close"):
                pos.price_source_at_close = used_source or price_src or None

            try:
                await ses.commit()
            except SQLAlchemyError:
                await ses.rollback()

            if not DRY_RUN:
                try:
                    _wallet_sol_balance += pos.qty / 1e9
                except Exception:
                    pass

            _stats["sold"] += 1
            sells_done += 1
            # no seguimos evaluando más reglas para esta posición
            continue

        # ③ Evaluar salida con el precio disponible (puede ser None)
        if not await _should_exit(pos, price, now, liq_now=liq_now):
            # si tenemos precio, actualiza el máximo de PnL observado (para trailing)
            try:
                if price is not None and pos.buy_price_usd:
                    pnl_pct = (price - pos.buy_price_usd) / pos.buy_price_usd * 100
                    if pnl_pct > (pos.highest_pnl_pct or 0.0):
                        pos.highest_pnl_pct = float(pnl_pct)
                        await ses.commit()
            except Exception:
                try:
                    await ses.rollback()
                except Exception:
                    pass
            continue

        # ④ SELL — seller.sell hará su propio cálculo robusto de precio
        sell_resp = await seller.sell(
            pos.address,
            pos.qty,
            token_mint=mint_key,
            price_hint=price,            # el que calculaste en el monitor (puede ser None)
            price_source_hint=price_src, # "jup_batch" | "jup_single" | "jup_critical" | "dex_full" | None
        )
        pos.closed = True
        pos.closed_at = now

        # Precio realmente usado para cerrar (si seller lo resolvió)
        used_close  = (sell_resp or {}).get("price_used_usd")
        used_source = (sell_resp or {}).get("price_source_close")

        # Persistencia de precio de cierre y fuente
        if used_close is not None:
            try:
                pos.close_price_usd = float(used_close)  # incluye fallback_buy si aplicó
            except Exception:
                pos.close_price_usd = price if price is not None else None
        else:
            pos.close_price_usd = price if price is not None else None

        if hasattr(pos, "price_source_at_close"):
            pos.price_source_at_close = used_source or price_src or None

        pos.exit_tx_sig = (sell_resp or {}).get("signature")

        # ⑤ PnL → calcula solo si hay ambos precios
        pnl_pct = (
            None
            if pos.close_price_usd is None or pos.buy_price_usd is None
            else (pos.close_price_usd - pos.buy_price_usd) / pos.buy_price_usd * 100
        )
        _stats["sold"] += 1
        sells_done += 1

        # contadores de fuentes de cierre
        if used_source in close_source_counts:
            close_source_counts[used_source] += 1  # type: ignore
        elif used_source is None:
            close_source_counts["none"] += 1
        else:
            close_source_counts[used_source] = close_source_counts.get(used_source, 0) + 1  # type: ignore

        # persistencia
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

    # ⑥ Log de métricas del ciclo (salud)
    try:
        pct_with = (positions_with_price / total * 100.0) if total else 0.0
        pct_without = 100.0 - pct_with if total else 0.0

        log.info(
            "📊 Monitor: con precio %.1f%% (sin %.1f%%) | consult srcs: batch=%d single=%d crit=%d dex=%d none=%d | cierres: batch=%d single=%d crit=%d dex=%d fb=%d none=%d | ventas=%d",
            pct_with, pct_without,
            consult_source_counts.get("jup_batch", 0),
            consult_source_counts.get("jup_single", 0),
            consult_source_counts.get("jup_critical", 0),
            consult_source_counts.get("dex_full", 0),
            consult_source_counts.get("none", 0),
            close_source_counts.get("jup_batch", 0),
            close_source_counts.get("jup_single", 0),
            close_source_counts.get("jup_critical", 0),
            close_source_counts.get("dex_full", 0),
            close_source_counts.get("fallback_buy", 0),
            close_source_counts.get("none", 0),
            sells_done,
        )

        log.debug(
            "📊 Detalle: batch %d/%d, fallback %d, crítico %d/%d, dex_full %d, sin precio %d, ventas %d",
            batch_resolved,
            total,
            fallback_resolved,
            critical_resolved,
            _CRIT_MAX,
            dex_full_resolved,
            no_price,
            sells_done,
        )
    except Exception:
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
