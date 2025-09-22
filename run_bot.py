# memebot3/run_bot.py
"""
⏯️  Orquestador principal del sniper MemeBot 3
──────────────────────────────────────────────
Última revisión · 2025-09-22

Novedades importantes
─────────────────────
• Sin “fuga” de etiquetas: no se persisten positivos en T0 (solo negativos inmediatos).
• Persistencia al cerrar posiciones (real y DRY-RUN) usando el vector T0 guardado.
• Shadow simulation en MODO REAL (REAL_SHADOW_SIM=true).
• Flag FORCE_JUP_IN_MONITOR para forzar Jupiter-first en monitor.
• Umbral IA dinámico con suavizado (MIN_THRESHOLD_CHANGE).
• Gate IA con AI_TH (nuevo) y soft-score mínimo BUY_SOFT_SCORE_MIN (nuevo).
• Guard de pool: DEX_WHITELIST + (si router) ruta Jupiter requerida.
• Rate limiter de BUY: BUY_RATE_LIMIT_N / BUY_RATE_LIMIT_WINDOW_S (no bloqueante).
• Nuevas métricas: appended_at_close, appended_shadow, filtered_immediate_0.

Revisión 2025-09-22
───────────────────
• Reentrenamiento: se amplía la ventana de disparo de <10 a <15 minutos.
• Reentrenamiento: tras `reload_model()` ahora se re-lee y aplica en caliente el
  umbral recomendado desde `data/metrics/recommended_threshold.json` (o meta),
  respetando `MIN_THRESHOLD_CHANGE`, con logs “aplicado/ignorado”.
• Logs: se añade aviso “⏰ Ventana de retraining abierta (UTC=YYYY-mm-dd HH:MM)”.
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
    USE_JUPITER_PRICE,      # batch Jupiter
    FORCE_JUP_IN_MONITOR,   # ← NUEVO
    REAL_SHADOW_SIM,        # ← NUEVO
    MIN_THRESHOLD_CHANGE,   # ← NUEVO (suavizado umbral IA)
    WIN_PCT,                # ← para etiquetado al cierre (ratio, no %)
    # NUEVOS flags/umbrales
    AI_THRESHOLD as AI_TH_CFG,
    DEX_WHITELIST,
    REQUIRE_POOL_INITIALIZED,
    BUY_RATE_LIMIT_N,
    BUY_RATE_LIMIT_WINDOW_S,
)
from config import exits  # take-profit / stop-loss

MIN_MARKET_CAP_USD = CFG.MIN_MARKET_CAP_USD
MAX_MARKET_CAP_USD = CFG.MAX_MARKET_CAP_USD
BUY_SOFT_SCORE_MIN = CFG.BUY_SOFT_SCORE_MIN  # nuevo

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
# Router Jupiter (opcional) para has_route/impact
try:
    # Debe exponer get_quote(input_mint: str, output_mint: str, amount_sol: float)
    # con atributos: ok: bool, price_impact_bps: int|None
    from fetcher import jupiter_router as jupiter  # type: ignore
    _JUP_ROUTER_AVAILABLE = True
except Exception:
    jupiter = None  # type: ignore
    _JUP_ROUTER_AVAILABLE = False

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


# ╭─────────────────────── helpers: ventanas / bloqueos ──────────────────────╮
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
            try:
                a, b = int(a), int(b)
            except Exception:
                continue
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
    return sorted(windows)

def _in_ranges(now_local: dt.datetime, ranges: List[Tuple[int, int]]) -> bool:
    if not ranges:
        return False
    h = now_local.hour
    for a, b in ranges:
        if a <= h <= b:
            return True
    return False

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
    if _in_windows(now_local, windows):
        return 15 * 60  # estamos dentro; reintento “suave”
    candidates: List[int] = []
    for a, _ in windows:
        if a >= h:
            delta = (a - h) * 3600 - m * 60 - s
            candidates.append(delta if delta >= 0 else 0)
        else:
            delta = (24 - h + a) * 3600 - m * 60 - s
            candidates.append(delta)
    return min(candidates) if candidates else 3600

# Ventanas permitidas
_TRADING_HOURS       = _parse_hours(os.getenv("TRADING_HOURS", ""))
_TRADING_HOURS_EXTRA = _parse_hours(os.getenv("TRADING_HOURS_EXTRA", ""))
_USE_EXTRA_HOURS     = os.getenv("USE_EXTRA_HOURS", "false").lower() == "true"
# Horas bloqueadas
_BLOCK_HOURS         = _parse_hours(os.getenv("BLOCK_HOURS", ""))

_REQUIRE_JUP_FOR_BUY = os.getenv("REQUIRE_JUPITER_FOR_BUY", "true").lower() == "true"

def _in_trading_window(now_local: Optional[dt.datetime] = None) -> bool:
    """True si (ventanas vacías o dentro de ventanas) y NO en horas bloqueadas."""
    now_local = now_local or dt.datetime.now()
    windows = list(_TRADING_HOURS)
    if _USE_EXTRA_HOURS:
        windows += list(_TRADING_HOURS_EXTRA)
    allowed_by_window = _in_windows(now_local, windows) if windows else True
    blocked = _in_ranges(now_local, _BLOCK_HOURS) if _BLOCK_HOURS else False
    return allowed_by_window and not blocked

def _delay_until_window(now_local: Optional[dt.datetime] = None) -> int:
    """
    Segundos hasta la próxima franja permitida (considera ventanas y bloqueos).
    Si ya está permitido, devuelve 0. Busca al siguiente “inicio de hora”.
    """
    now_local = now_local or dt.datetime.now()
    if _in_trading_window(now_local):
        return 0

    windows = list(_TRADING_HOURS)
    if _USE_EXTRA_HOURS:
        windows += list(_TRADING_HOURS_EXTRA)

    base = now_local.replace(minute=0, second=0, microsecond=0)
    # Buscamos en los próximos 48 saltos horarios una hora permitida
    for i in range(0, 48):
        # si ya estamos en xx:00 exacto, el siguiente turno es +0, si no, +1
        cand = base + dt.timedelta(hours=i + (0 if now_local == base else 1))
        ok_window = _in_windows(cand, windows) if windows else True
        blocked = _in_ranges(cand, _BLOCK_HOURS) if _BLOCK_HOURS else False
        if ok_window and not blocked:
            delta = (cand - now_local).total_seconds()
            return int(max(30, delta))
    return 15 * 60  # fallback improbable


# ╭─────────────────────── Rate limiter de BUY ───────────────────────────────╮
class _BuyLimiter:
    """Leaky-bucket simple no bloqueante para BUY."""
    def __init__(self, max_hits: int, window_s: int):
        self.max_hits = max(1, int(max_hits))
        self.window_s = max(1, int(window_s))
        self._ts = deque()  # timestamps monotonic de BUYs concedidos

    def allow(self, n: int = 1) -> bool:
        now = time.monotonic()
        # purge fuera de ventana
        while self._ts and now - self._ts[0] > self.window_s:
            self._ts.popleft()
        if len(self._ts) + n <= self.max_hits:
            for _ in range(n):
                self._ts.append(now)
            return True
        return False

    def current(self) -> int:
        now = time.monotonic()
        while self._ts and now - self._ts[0] > self.window_s:
            self._ts.popleft()
        return len(self._ts)

_BUY_LIMITER = _BuyLimiter(BUY_RATE_LIMIT_N, BUY_RATE_LIMIT_WINDOW_S)


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
AI_THRESHOLD         = AI_TH_CFG  # usar AI_TH nuevo por defecto; puede sobreescribirse abajo

# Kill-switches (riesgo) — valores por defecto razonables
_EARLY_DROP_PCT   = float(os.getenv("KILL_EARLY_DROP_PCT", "45"))
_EARLY_WINDOW_S   = int(os.getenv("KILL_EARLY_WINDOW_S", "90"))
_LIQ_CRUSH_FRAC   = float(os.getenv("KILL_LIQ_FRACTION", "0.70"))  # requiere buy_liquidity_usd + liq tick

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
            if not isinstance(val, (int, float)):
                val = meta.get("threshold")
            if isinstance(val, (int, float)):
                return float(val)
    except Exception:
        pass
    return None

_thr_override = _load_ai_threshold_override()
if _thr_override is not None:
    old = AI_THRESHOLD
    # suavizado: solo aplicar si cambio >= MIN_THRESHOLD_CHANGE
    if abs(float(_thr_override) - float(old)) >= float(MIN_THRESHOLD_CHANGE):
        AI_THRESHOLD = float(_thr_override)
        log.info("🎯 AI_THRESHOLD override aplicado: %.3f (antes=%.3f, Δ=%.3f≥%.3f)",
                 AI_THRESHOLD, old, AI_THRESHOLD - old, MIN_THRESHOLD_CHANGE)
    else:
        log.info("🎯 AI_THRESHOLD override ignorado por suavizado: rec=%.3f, actual=%.3f, Δ=%.3f<%.3f",
                 float(_thr_override), float(old), float(_thr_override) - float(old), float(MIN_THRESHOLD_CHANGE))

# ╭─────────────────────── Estado global ─────────────────────────────────────╮
_wallet_sol_balance: float = 0.0
_last_wallet_check   : float = 0.0

# Vectores de features pendientes de etiquetar (por mint/address)
_pending_ai_vectors: Dict[str, List[float]] = {}  # address → feature_vector

# NUEVO: shadow positions (modo real)
_shadow_positions: Dict[str, Dict[str, object]] = {}  # address → {"vec":..., "opened_at":..., "buy_price_usd":...}

_stats = {
    "raw_discovered": 0,
    "incomplete":     0,
    "filtered_out":   0,
    "ai_pass":        0,
    "bought":         0,
    "sold":           0,
    "requeues":       0,
    "requeue_success": 0,
    # Nuevos contadores
    "appended_at_close": 0,
    "appended_shadow":   0,
    "filtered_immediate_0": 0,
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
    dex_id = tok.get("dex_id") or tok.get("dexId") or tok.get("dexid")
    dex_id_norm = _norm_dex_id(dex_id)
    log.debug(
        "⛳ Nuevo %s | liq=%s vol24h=%s mcap=%s age=%s | dexId=%s",
        tok.get("symbol") or addr[:4],
        _fmt(tok.get("liquidity_usd"), "{:.0f}"),
        _fmt(tok.get("volume_24h_usd"), "{:.0f}"),
        _fmt(tok.get("market_cap_usd"), "{:.0f}"),
        _fmt(tok.get("age_min"), "{:.1f}m"),
        dex_id_norm or "?",
    )


# ╭─────────────────────── Shadow helpers ────────────────────────────────────╮
async def _open_shadow(addr: str, vec: List[float], price_hint: Optional[float] = None) -> None:
    """Crea una shadow position (solo modo real) cuando pasa IA pero no se compra."""
    if DRY_RUN or not REAL_SHADOW_SIM:
        return
    try:
        price = price_hint
        if price is None:
            # Jupiter primero; si falla, fallback a Dex/GT “solo precio”
            try:
                price = await price_service.get_price_usd(addr)
            except Exception:
                price = None
            if price is None:
                tok = await price_service.get_price(addr, use_gt=True, price_only=True)
                price = float(tok.get("price_usd")) if tok and tok.get("price_usd") else None
        _shadow_positions[addr] = {
            "vec": vec,
            "opened_at": utc_now(),
            "buy_price_usd": float(price) if price is not None else None,
        }
        log.info("👻 Shadow creada: %s (buy_price_usd=%s)", addr[:6], _fmt(price))
    except Exception as exc:
        log.debug("open_shadow %s → %s", addr[:6], exc)

async def _tick_shadows() -> None:
    """Revisa sombras y cierra las que hayan alcanzado MAX_HOLDING_H; persiste label."""
    if not _shadow_positions:
        return
    to_delete: List[str] = []
    now = utc_now()
    for addr, sd in _shadow_positions.items():
        opened = sd.get("opened_at")
        if not opened:
            to_delete.append(addr); continue
        age_h = (now - opened).total_seconds() / 3600.0
        if age_h < MAX_HOLDING_H:
            continue

        buy_price = sd.get("buy_price_usd")
        # Obtener precio de cierre (Jupiter-first forzado)
        try:
            close_price = await price_service.get_price_usd(addr)
        except Exception:
            close_price = None
        if close_price is None:
            try:
                tok = await price_service.get_price(addr, use_gt=True, price_only=True)
                close_price = float(tok.get("price_usd")) if tok and tok.get("price_usd") else None
            except Exception:
                close_price = None

        # Calcular label
        label = 0
        if buy_price and close_price:
            pnl_ratio = (close_price - buy_price) / buy_price
            label = 1 if pnl_ratio >= WIN_PCT else 0
        vec = sd.get("vec")
        if vec:
            try:
                store_append(vec, label)
                _stats["appended_shadow"] += 1
            except Exception as exc:
                log.debug("store_append shadow %s → %s", addr[:6], exc)

        to_delete.append(addr)

    for addr in to_delete:
        _shadow_positions.pop(addr, None)


# ───────────────────────── helpers pool/route/DEX ───────────────────────────
def _norm_dex_id(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).strip().lower().replace(" ", "")
    # normalizaciones rápidas típicas
    s = s.replace("_", "").replace("-", "")
    return s or None

async def _has_jupiter_route(output_mint: str, amount_sol: float) -> Optional[bool]:
    """Devuelve True/False si hay ruta según router; None si router no disponible/error."""
    if not _JUP_ROUTER_AVAILABLE or jupiter is None:
        return None
    try:
        SOL_MINT = "So11111111111111111111111111111111111111112"
        amt = max(0.005, min(float(amount_sol or 0.01), 0.2))
        q = await jupiter.get_quote(input_mint=SOL_MINT, output_mint=output_mint, amount_sol=amt)
        return bool(getattr(q, "ok", False))
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────────────
# run_bot.py  — _evaluate_and_buy
# ────────────────────────────────────────────────────────────────────────────
async def _evaluate_and_buy(token: dict, ses: SessionLocal) -> None:
    """Evalúa un token y, si pasa los filtros + IA, lanza la compra."""
    global _wallet_sol_balance

    addr = token["address"]
    _stats["raw_discovered"] += 1

    # 0) — gate horario (24/7 si no hay ventanas; BLOCK_HOURS siempre aplica si define) —
    if not _in_trading_window():
        delay = max(30, _delay_until_window())
        # Motivo de log diferenciado
        if _BLOCK_HOURS and _in_ranges(dt.datetime.now(), _BLOCK_HOURS):
            reason = "blocked_hour"
        else:
            reason = "off_hours"
        requeue(addr, reason=reason, backoff=delay)
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
        _stats["filtered_out"] += 1
        store_append(build_feature_vector(token), 0)
        _stats["filtered_immediate_0"] += 1
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
        _stats["filtered_immediate_0"] += 1

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
            _stats["filtered_immediate_0"] += 1
            eliminar_par(addr)
        return

    # 8) — señales caras —
    token["rug_score"]   = await rugcheck.check_token(addr)
    token["cluster_bad"] = await clusters.suspicious_cluster(addr)
    token["score_total"] = filters.total_score(token)

    # 9) — IA + soft score gate —
    vec = build_feature_vector(token)
    proba = should_buy(vec)
    if proba < AI_THRESHOLD:
        _stats["filtered_out"] += 1
        store_append(vec, 0)
        _stats["filtered_immediate_0"] += 1
        eliminar_par(addr)
        return

    if BUY_SOFT_SCORE_MIN > 0 and int(token.get("score_total") or 0) < int(BUY_SOFT_SCORE_MIN):
        log.debug("🪫 Soft score gate: %s score_total=%d < %d",
                  addr[:6], int(token.get("score_total") or 0), BUY_SOFT_SCORE_MIN)
        _stats["filtered_out"] += 1
        store_append(vec, 0)
        _stats["filtered_immediate_0"] += 1
        eliminar_par(addr)
        return

    _stats["ai_pass"] += 1

    # IMPORTANTE: no etiquetar 1 en T0; guardamos el vector para el cierre
    _pending_ai_vectors[addr] = vec

    # 10) — importe —
    amount_sol = _compute_trade_amount()
    if amount_sol < MIN_BUY_SOL:
        # Shadow si pasa IA pero no se compra por importe
        await _open_shadow(addr, vec, price_hint=token.get("price_usd"))
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

    # 11.5) — Guard de pool (DEX whitelist) + ruta Jupiter (si router) —
    if REQUIRE_POOL_INITIALIZED:
        dex_id_norm = _norm_dex_id(token.get("dex_id") or token.get("dexId"))
        if dex_id_norm and DEX_WHITELIST and dex_id_norm not in DEX_WHITELIST:
            log.info("🛑 BUY bloqueado: DEX no whitelisted (dex=%s, allow=%s)", dex_id_norm, ",".join(DEX_WHITELIST))
            _stats["filtered_out"] += 1
            store_append(vec, 0)
            _stats["filtered_immediate_0"] += 1
            eliminar_par(addr)
            return

        # Mejor aún: comprobar ruta ejecutable (si router disponible)
        has_route = await _has_jupiter_route(addr, amount_sol)

        # ⚠️ Cambio clave: solo BLOQUEAMOS si la política exige Jupiter.
        if _REQUIRE_JUP_FOR_BUY:
            if has_route is False:
                log.info("🛑 BUY bloqueado: sin ruta Jupiter (mint=%s, reason=no_route)", addr[:6])
                requeue(addr, reason="no_route", backoff=90)
                _stats["requeues"] += 1
                return
        else:
            # Data acquisition / DRY-RUN: seguimos aunque Jupiter aún no tenga ruta
            if has_route is False:
                log.debug("[run_bot] sin ruta Jupiter (mint=%s) pero REQUIRE_JUPITER_FOR_BUY=false → continúo", addr[:6])
        # has_route is None → router no disponible: no bloqueamos aquí

    # 12) — “Exigir Jupiter” para comprar (solo precio) —
    if _REQUIRE_JUP_FOR_BUY:
        try:
            jtok = await price_service.get_price(addr, price_only=True)  # usa flag interno
        except Exception:
            jtok = None
        if not jtok or jtok.get("price_usd") in (None, 0):
            log.info("🛑 BUY bloqueado (sin precio) %s", addr[:6])
            # Shadow si pasa IA pero no hay precio Jupiter
            await _open_shadow(addr, vec, price_hint=token.get("price_usd"))
            eliminar_par(addr)
            return

    # 12.5) — Rate limiter de BUY (no bloqueante): cooldown si no permite —
    if not _BUY_LIMITER.allow():
        cur = _BUY_LIMITER.current()
        log.info("⏳ BUY en cooldown por rate limit (%d/%ds, usado=%d)",
                 BUY_RATE_LIMIT_N, BUY_RATE_LIMIT_WINDOW_S, cur)
        # backoff prudente: mitad de ventana con jitter
        back = max(20, int(BUY_RATE_LIMIT_WINDOW_S * random.uniform(0.4, 0.7)))
        requeue(addr, reason="buy_rate_limit", backoff=back)
        _stats["requeues"] += 1
        return

    # 13) — BUY —
    try:
        if DRY_RUN:
            buy_resp = await buyer.buy(
                addr, amount_sol,
                price_hint=token.get("price_usd"),
                token_mint=token.get("address") or addr,
                liquidity_usd=token.get("liquidity_usd"),
            )
        else:
            buy_resp = await buyer.buy(addr, amount_sol, token_mint=addr)
    except Exception as exc:
        log.error("buyer.buy %s → %s", addr[:4], exc, exc_info=True)
        # Shadow si falla la compra real
        await _open_shadow(addr, vec, price_hint=token.get("price_usd"))
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


# ╭─────────────────────── Exit strategy (monitor) ───────────────────────────╮
async def _load_open_positions(ses: SessionLocal) -> Sequence[Position]:
    stmt = select(Position).where(Position.closed.is_(False))
    return (await ses.execute(stmt)).scalars().all()


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


# ────────────────────────── Persistir label al cierre ───────────────────────
def _persist_dataset_at_close(pos: Position, price_used: Optional[float]) -> None:
    """Usa el vector T0 en memoria para persistir el label al cerrar la posición."""
    try:
        vec = _pending_ai_vectors.pop(pos.address, None)
        if vec is None:
            return
        buy = getattr(pos, "buy_price_usd", None)
        close = price_used if price_used is not None else getattr(pos, "close_price_usd", None)
        if not buy or not close:
            label = 0
        else:
            pnl_ratio = (float(close) - float(buy)) / float(buy)  # ratio, NO %
            label = 1 if pnl_ratio >= float(WIN_PCT) else 0
        store_append(vec, label)
        _stats["appended_at_close"] += 1
    except Exception as exc:
        log.debug("persist_dataset_at_close %s → %s", pos.address[:6], exc)


# ────────────────────────── Monitor de posiciones ───────────────────────────
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
            return True

        opened: dt.datetime | None = None
        try:
            if isinstance(opened_raw, dt.datetime):
                opened = opened_raw if opened_raw.tzinfo else opened_raw.replace(tzinfo=dt.timezone.utc)
            elif isinstance(opened_raw, str):
                opened = parse_iso_utc(opened_raw)
        except Exception:
            opened = None

        if opened is None:
            return True

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

        # FORZAR Jupiter-first si el flag está activo
        if FORCE_JUP_IN_MONITOR:
            prefer_dex = False

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
            # Camino preferente: Jupiter → Dex/GT
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

            # Persistencia dataset al cierre
            _persist_dataset_at_close(pos, used_close if used_close is not None else price)

            if not DRY_RUN:
                try:
                    _wallet_sol_balance += pos.qty / 1e9
                except Exception:
                    pass

            _stats["sold"] += 1
            sells_done += 1
            continue  # siguiente posición

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

        _stats["sold"] += 1
        sells_done += 1

        try:
            await ses.commit()
        except SQLAlchemyError:
            await ses.rollback()

        # Persistencia dataset al cierre
        _persist_dataset_at_close(pos, used_close if used_close is not None else price)

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
            and now.minute < 15   # ← ampliamos ventana (antes: < 10)
        ):
            # Log explícito de entrada en ventana
            try:
                log.info("⏰ Ventana de retraining abierta (UTC=%s)", now.strftime("%Y-%m-%d %H:%M"))
            except Exception:
                pass

            try:
                if retrain_if_better():
                    # 1) Recargar modelo recién guardado
                    reload_model()

                    # 2) Releer y aplicar override de umbral recomendado en caliente
                    _thr_override2 = _load_ai_threshold_override()
                    if _thr_override2 is not None:
                        global AI_THRESHOLD  # aplicar sobre el gate de IA en ejecución
                        old = AI_THRESHOLD
                        if abs(float(_thr_override2) - float(old)) >= float(MIN_THRESHOLD_CHANGE):
                            AI_THRESHOLD = float(_thr_override2)
                            log.info(
                                "🎯 AI_THRESHOLD override aplicado: %.3f (antes=%.3f, Δ=%.3f≥%.3f)",
                                AI_THRESHOLD, old, AI_THRESHOLD - old, MIN_THRESHOLD_CHANGE
                            )
                        else:
                            log.info(
                                "🎯 AI_THRESHOLD override ignorado por suavizado: rec=%.3f, actual=%.3f, Δ=%.3f<%.3f",
                                float(_thr_override2), float(old),
                                float(_thr_override2) - float(old), float(MIN_THRESHOLD_CHANGE)
                            )

                    log.info("🐢 Retrain completo; modelo recargado en memoria")
            except Exception as exc:
                log.error("Retrain error: %s", exc)

            # Evitar disparos repetidos dentro de la misma hora
            await asyncio.sleep(3600)
        else:
            await asyncio.sleep(300)


# ╭─────────────────────── Main loop ─────────────────────────────────────────╮
async def main_loop() -> None:
    ses             = SessionLocal()
    last_discovery  = 0.0

    # ── Banner de estado de ventanas/bloqueos al arrancar ──
    now_local = dt.datetime.now()
    windows = list(_TRADING_HOURS) + (list(_TRADING_HOURS_EXTRA) if _USE_EXTRA_HOURS else [])
    has_windows = bool(windows)
    is_blocked  = _in_ranges(now_local, _BLOCK_HOURS) if _BLOCK_HOURS else False
    is_allowed  = _in_trading_window(now_local)

    if not has_windows and not _BLOCK_HOURS:
        log.info("🕒 Ventanas: 24/7 (sin TRADING_HOURS definidos); sin BLOCK_HOURS.")
    else:
        if is_allowed:
            if is_blocked:
                log.warning("⛔ Estado horario inconsistente: marcado como allowed pero en BLOCK_HOURS.")
            else:
                log.info("🟢 Inicio en hora PERMITIDA (ventanas aplicadas%s).",
                         " + EXTRA" if _USE_EXTRA_HOURS else "")
        else:
            reason = "BLOCK_HOURS" if is_blocked else "fuera de ventana"
            delay  = _delay_until_window(now_local)
            log.info("⏸️  Inicio en hora NO operable (%s). Próximo intento en ~%ds.", reason, delay)

    log.info(
        "Ready (discover=%ss, batch=%s, sleep=%ss, DRY_RUN=%s, AI_THRESHOLD=%.2f)",
        DISCOVERY_INTERVAL,
        VALIDATION_BATCH_SIZE,
        SLEEP_SECONDS,
        DRY_RUN,
        AI_THRESHOLD,
    )
    # Banner de nuevos flags/umbrales
    log.info(
        "⚙️  Config extra: soft_score_min=%d · dex_whitelist=%s · require_pool_initialized=%s · buy_rl=%d/%ds",
        BUY_SOFT_SCORE_MIN,
        ",".join(DEX_WHITELIST) or "(none)",
        str(REQUIRE_POOL_INITIALIZED),
        BUY_RATE_LIMIT_N,
        BUY_RATE_LIMIT_WINDOW_S,
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

        # 4.5) Shadows (modo real)
        if not DRY_RUN and REAL_SHADOW_SIM:
            try:
                await _tick_shadows()
            except Exception as exc:
                log.debug("tick_shadows → %s", exc)

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
