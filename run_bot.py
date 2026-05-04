# memebot3/run_bot.py
"""
⏯️  Orquestador principal del sniper MemeBot 3
──────────────────────────────────────────────
Última revisión · 2025-09-22 (+ mejoras exits PnL)

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

Mejoras PnL (implementadas en este archivo)
────────────────────────────────────────────
• TP parcial REAL en el orquestador: al tocar TAKE_PROFIT_PCT vende una fracción UNA vez,
  marca partial_taken=True y deja el resto a trailing/SL/timeout.
• NO_PUMP stop: si tras X minutos nunca superó +Y% → salir (reduce trades muertos).
• TIME_STOP: si tras X minutos sigue “sin despegar” → salir.
• Fix: eliminado el “fake” SOL balance update (_wallet_sol_balance += pos.qty/1e9).
  Tras vender en real-mode se refresca balance real vía RPC.
• exit_reason coherente para cierres (TP/SL/trailing/timeout/no_pump/time_stop/early_drop/...).

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
import hashlib
import json
import logging
import math
import os
import random
import socket
import sys
import time
from collections import deque
from types import SimpleNamespace
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


def _to_float(val, default: float | None = None) -> float | None:
    try:
        if val is None:
            return default
        out = float(val)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default

# Reduce ruido de librerías verbosas
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

# ───────── SQLAlchemy (async) ────────────────────────────────────────────────
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.inspection import inspect

# ───────── Config & exits ───────────────────────────────────────────────────
from config.config import (  # noqa: E402 – after stdlib
    CFG,
    PROJECT_ROOT,
    BANNED_CREATORS,
    INCOMPLETE_RETRIES,
    USE_JUPITER_PRICE,      # batch Jupiter
    FORCE_JUP_IN_MONITOR,   # ← NUEVO
    REAL_SHADOW_SIM,        # ← NUEVO
    MIN_THRESHOLD_CHANGE,   # ← NUEVO (suavizado umbral IA)
    ML_POSITIVE_PNL_RATIO,  # ← para etiquetado ML al cierre (ratio, no %)
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
from runtime.command_bus import (  # noqa: E402
    DEFAULT_BOT_ID as CONTROL_DEFAULT_BOT_ID,
    STATUS_DONE as COMMAND_STATUS_DONE,
    STATUS_FAILED as COMMAND_STATUS_FAILED,
    STATUS_REJECTED as COMMAND_STATUS_REJECTED,
    claim_next_pending_command,
    complete_command,
)
from runtime.state_models import RuntimeStateSnapshot  # noqa: E402
from runtime.state_publisher import count_open_positions, publish_runtime_state  # noqa: E402
from runtime.single_instance import SingleInstanceLock, SingleInstanceLockError  # noqa: E402
from runtime.fast_enrichment import enrich_fast  # noqa: E402
from runtime.hot_queue import GLOBAL_HOT_QUEUE  # noqa: E402
from runtime import live_canary  # noqa: E402
from runtime.position_limits import evaluate_lane_position_limit  # noqa: E402
from runtime.social_enrichment_queue import schedule_social_enrichment  # noqa: E402

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
import analytics.sizing as entry_sizing  # noqa: E402
import analytics.exit_policy as exit_policy  # noqa: E402
import analytics.strategy_runtime as strategy_runtime  # noqa: E402
import analytics.research_runtime as research_runtime  # noqa: E402
from analytics.green_sniper_gate import apply_green_sniper_context, evaluate_green_sniper  # noqa: E402
from analytics.green_sniper_rank_guard import evaluate_green_sniper_rank_guard  # noqa: E402
from analytics.green_sniper_risk_guard import evaluate_green_sniper_risk_guard  # noqa: E402
from analytics.green_sniper_sizing import compute_green_sniper_sizing  # noqa: E402
from analytics.research_rank_canary import apply_research_rank_canary_context, evaluate_research_rank_canary  # noqa: E402
from analytics.profit_pnl_guard import evaluate_profit_pnl_guard  # noqa: E402
from analytics.ml_policy import decide_ml_action  # noqa: E402
from analytics.risk_predict import predict_risk  # noqa: E402
from analytics.ev_predict import predict_ev  # noqa: E402
from analytics.post_partial_experiment import refresh_snapshot as refresh_post_partial_experiment_snapshot  # noqa: E402
from analytics.reporting import (  # noqa: E402
    build_baseline_snapshot,
    render_baseline_markdown,
    render_edge_markdown,
    summarize_edge,
)
from analytics.ai_predict import should_buy, reload_model, model_runtime_status  # noqa: E402

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
from utils.data_utils import sanitize_token_data, apply_default_values, prepare_token_for_db  # noqa: E402
from utils.logger import enable_file_logging, warn_if_nulls, log_funnel  # noqa: E402
from utils.runtime_telemetry import (  # noqa: E402
    log_buy_event,
    log_execution_event,
    log_ml_decision_event,
    log_ml_policy_decision_event,
    log_regime_health_event,
    log_strategy_decision_event,
)
from utils.solana_rpc import get_sol_balance  # noqa: E402
from utils.time import utc_now, parse_iso_utc  # noqa: E402
from trade_pnl import apply_partial_fill, summarize_trade, total_pnl_ratio_from_record  # noqa: E402
from utils.sol_price import get_sol_usd  # noqa: E402

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
if bool(getattr(CFG, "STRATEGY_OPTIMIZATION_LOCK", True)) and not DRY_RUN:
    raise SystemExit("STRATEGY_OPTIMIZATION_LOCK=true blocks live runtime; start in DRY_RUN/paper mode")
_PROCESS_LOCK: SingleInstanceLock | None = None


def _acquire_process_lock() -> None:
    global _PROCESS_LOCK
    if _PROCESS_LOCK is not None and _PROCESS_LOCK.is_acquired:
        return

    lock = SingleInstanceLock(PROJECT_ROOT / "data" / "run_bot.lock")
    lock.acquire(
        payload={
            "argv": sys.argv,
            "dry_run": bool(DRY_RUN),
            "pid": os.getpid(),
            "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
    )
    _PROCESS_LOCK = lock


def _release_process_lock() -> None:
    global _PROCESS_LOCK
    if _PROCESS_LOCK is None:
        return
    _PROCESS_LOCK.release()
    _PROCESS_LOCK = None

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
try:
    EVALUATE_TOKEN_TIMEOUT_S = max(0.0, float(os.getenv("EVALUATE_TOKEN_TIMEOUT_S", "120")))
except Exception:
    EVALUATE_TOKEN_TIMEOUT_S = 120.0

TP_PCT        = exits.TAKE_PROFIT_PCT
SL_PCT        = exits.STOP_LOSS_PCT
TRAILING_PCT  = exits.TRAILING_PCT
MAX_HOLDING_H = exits.MAX_HOLDING_H
MAX_HARD_HOLD_H = CFG.MAX_HARD_HOLD_H
AI_THRESHOLD         = AI_TH_CFG  # usar AI_TH nuevo por defecto; puede sobreescribirse abajo

# Kill-switches / exits (config unificada)
_EARLY_DROP_PCT = float(CFG.EARLY_DROP_KILL_PCT or 0.0)
_EARLY_WINDOW_S = int(max(0.0, float(CFG.EARLY_DROP_WINDOW_MIN or 0.0)) * 60.0)
_LIQ_CRUSH_FRAC = float(CFG.KILL_LIQ_FRACTION or 0.0)
TP_PARTIAL_ENABLED = bool(CFG.TP_PARTIAL_ENABLED)
TP_PARTIAL_FRACTION = max(0.05, min(0.95, float(CFG.TP_PARTIAL_FRACTION or 0.40)))
TP_PARTIAL_MIN_REMAIN_LAMPORTS = max(0, int(CFG.TP_PARTIAL_MIN_REMAIN_LAMPORTS or 1))
POST_PARTIAL_STOP_PCT = float(CFG.POST_PARTIAL_STOP_PCT or 0.0)
POST_PARTIAL_TRAILING_PCT = float(CFG.POST_PARTIAL_TRAILING_PCT or 0.0)
NO_PUMP_WINDOW_MIN = float(CFG.NO_PUMP_WINDOW_MIN or 0.0)
NO_PUMP_MIN_PNL_PCT = float(CFG.NO_PUMP_MIN_PNL_PCT or 0.0)
TIME_STOP_MIN = float(CFG.TIME_STOP_MIN or 0.0)
TIME_STOP_MAX_PNL_PCT = float(CFG.TIME_STOP_MAX_PNL_PCT or 0.0)
TIME_STOP_MIN_PEAK_PCT = float(CFG.TIME_STOP_MIN_PEAK_PCT or 0.0)

# ╭─────────────────────── Carga de AI_THRESHOLD recomendado ─────────────────╮
def _load_ai_threshold_override() -> Optional[float]:
    """
    Intenta leer:
      1) data/metrics/recommended_threshold.json → {"picked": 0.34, ...}
      2) modelo.meta.json → {"ai_threshold_recommended": 0.34, ...}
    """
    def _extract_ready_threshold(payload: dict[str, object], *keys: str) -> Optional[float]:
        if payload.get("activation_ready") is False:
            return None
        for key in keys:
            val = payload.get(key)
            if isinstance(val, (int, float)):
                return float(val)
        return None

    # 1) recommended_threshold.json junto a FEATURES_DIR/../metrics
    try:
        metrics_dir = CFG.FEATURES_DIR.parent / "metrics"
        thr_path = metrics_dir / "recommended_threshold.json"
        if thr_path.exists():
            data = json.loads(thr_path.read_text())
            val = _extract_ready_threshold(data, "picked")
            if val is not None:
                return val
    except Exception:
        pass

    # 2) meta del modelo
    try:
        meta_path = CFG.MODEL_PATH.with_suffix(".meta.json")
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            val = _extract_ready_threshold(meta, "ai_threshold_recommended", "threshold")
            if val is not None:
                return val
    except Exception:
        pass
    return None


def _apply_ai_threshold_override() -> dict[str, object]:
    global AI_THRESHOLD

    recommended = _load_ai_threshold_override()
    if recommended is None:
        return {
            "recommended_threshold": None,
            "applied": False,
            "changed": False,
            "current_threshold": float(AI_THRESHOLD),
            "reason": "override_missing",
        }

    current = float(AI_THRESHOLD)
    delta = float(recommended) - current
    if abs(delta) < float(MIN_THRESHOLD_CHANGE):
        log.info(
            "AI_THRESHOLD override ignored by smoothing: rec=%.3f current=%.3f delta=%.3f<%.3f",
            float(recommended),
            current,
            delta,
            float(MIN_THRESHOLD_CHANGE),
        )
        return {
            "recommended_threshold": float(recommended),
            "applied": False,
            "changed": False,
            "current_threshold": current,
            "reason": "below_min_change",
        }

    AI_THRESHOLD = float(recommended)
    log.info(
        "AI_THRESHOLD override applied: %.3f (before=%.3f delta=%.3f>=%.3f)",
        float(AI_THRESHOLD),
        current,
        float(AI_THRESHOLD) - current,
        float(MIN_THRESHOLD_CHANGE),
    )
    return {
        "recommended_threshold": float(recommended),
        "applied": True,
        "changed": True,
        "current_threshold": float(AI_THRESHOLD),
        "reason": "applied",
    }


_apply_ai_threshold_override()


def _ml_gate_state() -> dict[str, object]:
    raw_mode = str(getattr(CFG, "ML_GATE_MODE", "legacy") or "legacy").strip().lower()
    if raw_mode not in {"legacy", "shadow", "enforce", "off", "lane_aware", "sizing_only", "risk_veto_only"}:
        raw_mode = "legacy"

    status = model_runtime_status()
    activation_ready_raw = status.get("activation_ready")
    activation_ready = bool(activation_ready_raw) if activation_ready_raw is not None else False

    if raw_mode == "off":
        enforce = False
    elif raw_mode == "shadow":
        enforce = False
    elif raw_mode == "enforce":
        enforce = activation_ready
    elif raw_mode in {"lane_aware", "sizing_only", "risk_veto_only"}:
        enforce = False
    else:
        enforce = True

    return {
        "mode": raw_mode,
        "enforce": bool(enforce),
        "activation_ready": activation_ready_raw,
        "model_loaded": bool(status.get("model_loaded")),
        "threshold_metric": status.get("threshold_metric"),
        "rows": status.get("rows"),
    }

# ╭─────────────────────── Estado global ─────────────────────────────────────╮
_wallet_sol_balance: float = 0.0
_last_wallet_check   : float = 0.0

# Vectores de features pendientes de etiquetar (por mint/address)
_pending_ai_vectors: Dict[str, List[float]] = {}  # address → feature_vector

# NUEVO: shadow positions (modo real)
_shadow_positions: Dict[str, Dict[str, object]] = {}  # address → {"vec":..., "opened_at":..., "buy_price_usd":...}

_stats = {
    "raw_discovered": 0,
    "queue_added_total": 0,
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
_last_buy_at: Optional[dt.datetime] = None
_last_sell_at: Optional[dt.datetime] = None
_last_wallet_checked_at: Optional[dt.datetime] = None
_last_discovery_ok_at: Optional[dt.datetime] = None
_last_monitor_ok_at: Optional[dt.datetime] = None
_runtime_started_at: Optional[dt.datetime] = None
_runtime_process_state: str = "starting"
_runtime_last_error: Optional[str] = None
_runtime_last_error_at: Optional[dt.datetime] = None
_runtime_retrain_state: str = "idle"
_runtime_reports_refresh_state: str = "idle"
_runtime_discovery_paused: bool = False
_runtime_buys_paused: bool = False
_retrain_lock = asyncio.Lock()
_reports_refresh_lock = asyncio.Lock()
_CONTROL_COMMAND_POLL_INTERVAL_S = 1.0
_RUNTIME_STATE_INTERVAL_S = 5
_RUNTIME_STATE_BOT_ID = CONTROL_DEFAULT_BOT_ID


def _queue_add_if_new(addr: str, retries: int | None = None) -> bool:
    added = bool(agregar_si_nuevo(addr, retries=retries))
    if added:
        _stats["queue_added_total"] += 1
    return added


def _record_buy_stat(at: Optional[dt.datetime] = None) -> None:
    global _last_buy_at
    _stats["bought"] += 1
    _last_buy_at = at or utc_now()


def _record_sell_stat(at: Optional[dt.datetime] = None) -> None:
    global _last_sell_at
    _stats["sold"] += 1
    _last_sell_at = at or utc_now()


def _note_runtime_error(context: str, exc: Exception | str) -> None:
    global _runtime_last_error, _runtime_last_error_at
    detail = str(exc)
    _runtime_last_error = f"{context}: {detail}"[:1000]
    _runtime_last_error_at = utc_now()


def _effective_runtime_process_state(now: Optional[dt.datetime] = None) -> str:
    now = now or utc_now()
    if _runtime_process_state != "running":
        return _runtime_process_state
    if _runtime_last_error_at is not None and (now - _runtime_last_error_at).total_seconds() <= 180:
        return "degraded"
    return "running"


def _remember_queue_context(addr: str, token: dict | None = None) -> None:
    if not addr or not token:
        return
    meta = lista_pares.meta(addr)
    if meta is None:
        return
    discovered_via = str(token.get("discovered_via") or meta.get("discovered_via") or "").strip().lower()
    if discovered_via:
        meta["discovered_via"] = discovered_via
    dex_id = _norm_dex_id(token.get("dex_id") or token.get("dexId") or meta.get("dex_id"))
    if dex_id:
        meta["dex_id"] = dex_id
    discovered_at = token.get("discovered_at") or meta.get("discovered_at")
    if discovered_at is not None:
        meta["discovered_at"] = discovered_at
    symbol = token.get("symbol") or meta.get("symbol")
    if symbol:
        meta["symbol"] = str(symbol)
    entry_regime = token.get("entry_regime") or meta.get("entry_regime")
    if entry_regime:
        meta["entry_regime"] = str(entry_regime)


def _requeue_with_stats(addr: str, *, reason: str = "", backoff: int | None = None, token: dict | None = None) -> bool:
    _remember_queue_context(addr, token)
    queued = requeue(addr, reason=reason, backoff=backoff)
    if queued:
        _stats["requeues"] += 1
    return bool(queued)


def _ensure_requeue_with_stats(addr: str, *, reason: str = "", backoff: int | None = None, token: dict | None = None) -> bool:
    if lista_pares.meta(addr) is None:
        _queue_add_if_new(addr)
    _remember_queue_context(addr, token)
    return _requeue_with_stats(addr, reason=reason, backoff=backoff, token=token)


def _remove_from_queue_if_present(addr: str) -> None:
    if lista_pares.meta(addr) is not None:
        eliminar_par(addr)


def _apply_strategy_size_cap(
    size_decision: entry_sizing.EntrySizingDecision,
    size_cap_multiplier: float | None,
) -> entry_sizing.EntrySizingDecision:
    if size_cap_multiplier is None:
        return size_decision
    cap = max(0.0, float(size_cap_multiplier))
    if float(size_decision.multiplier) <= cap:
        return size_decision
    notes = tuple([*size_decision.notes, f"strategy_cap_{cap:.2f}"])
    bucket = "recovery" if cap <= float(getattr(CFG, "SIZE_MIN_MULTIPLIER", 0.10) or 0.10) else size_decision.bucket
    return entry_sizing.EntrySizingDecision(
        regime=size_decision.regime,
        quality_points=int(size_decision.quality_points),
        bucket=bucket,
        multiplier=float(cap),
        amount_sol=float(size_decision.amount_sol),
        notes=notes,
    )


try:
    _POLICY_REJECT_DEDUP_TTL_S = max(0.0, float(os.getenv("POLICY_REJECT_DEDUP_TTL_S", "900")))
except Exception:
    _POLICY_REJECT_DEDUP_TTL_S = 900.0
try:
    _PUMPFUN_STREAM_COOLDOWN_NO_LIQ_S = max(0, int(float(os.getenv("PUMPFUN_STREAM_COOLDOWN_NO_LIQ_S", "600"))))
except Exception:
    _PUMPFUN_STREAM_COOLDOWN_NO_LIQ_S = 600
try:
    _PUMPFUN_STREAM_COOLDOWN_REJECT_S = max(0, int(float(os.getenv("PUMPFUN_STREAM_COOLDOWN_REJECT_S", "900"))))
except Exception:
    _PUMPFUN_STREAM_COOLDOWN_REJECT_S = 900
try:
    _GECKO_MIN_QUEUE_AGE_S = max(0, int(float(os.getenv("GECKO_MIN_QUEUE_AGE_S", "90"))))
except Exception:
    _GECKO_MIN_QUEUE_AGE_S = 90
try:
    _GECKO_MIN_QUEUE_ATTEMPTS = max(0, int(float(os.getenv("GECKO_MIN_QUEUE_ATTEMPTS", "2"))))
except Exception:
    _GECKO_MIN_QUEUE_ATTEMPTS = 2
try:
    _PUMP_EARLY_QUALITY_MIN_POINTS = max(0, int(float(os.getenv("PUMP_EARLY_QUALITY_MIN_POINTS", "0"))))
except Exception:
    _PUMP_EARLY_QUALITY_MIN_POINTS = 0
try:
    _PUMP_EARLY_QUALITY_BACKOFF_S = max(30, int(float(os.getenv("PUMP_EARLY_QUALITY_BACKOFF_S", "120"))))
except Exception:
    _PUMP_EARLY_QUALITY_BACKOFF_S = 120
try:
    _DEX_MATURE_QUALITY_MIN_POINTS = max(0, int(float(os.getenv("DEX_MATURE_QUALITY_MIN_POINTS", "0"))))
except Exception:
    _DEX_MATURE_QUALITY_MIN_POINTS = 0
try:
    _DEX_MATURE_QUALITY_BACKOFF_S = max(30, int(float(os.getenv("DEX_MATURE_QUALITY_BACKOFF_S", "180"))))
except Exception:
    _DEX_MATURE_QUALITY_BACKOFF_S = 180
_RESEARCH_SHADOW_USE_GECKO = bool(getattr(CFG, "RESEARCH_SHADOW_USE_GECKO", False))
_PUMPFUN_PRICE_USE_GECKO = bool(getattr(CFG, "PUMPFUN_PRICE_USE_GECKO", False))


def _env_float(name: str, default: float = 0.0) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or not str(raw).strip():
            return float(default)
        return float(raw)
    except Exception:
        return float(default)


_DEX_MATURE_QUALITY_MIN_AGE_MIN = max(0.0, _env_float("DEX_MATURE_QUALITY_MIN_AGE_MIN", 0.0))
_DEX_MATURE_QUALITY_MIN_LIQUIDITY_USD = max(0.0, _env_float("DEX_MATURE_QUALITY_MIN_LIQUIDITY_USD", 0.0))
_DEX_MATURE_QUALITY_MIN_VOLUME_USD_24H = max(0.0, _env_float("DEX_MATURE_QUALITY_MIN_VOLUME_USD_24H", 0.0))
_DEX_MATURE_QUALITY_MIN_MARKET_CAP_USD = max(0.0, _env_float("DEX_MATURE_QUALITY_MIN_MARKET_CAP_USD", 0.0))
_DEX_MATURE_QUALITY_MIN_HOLDERS = max(0, int(_env_float("DEX_MATURE_QUALITY_MIN_HOLDERS", 0.0)))
_DEX_MATURE_QUALITY_MIN_SCORE_TOTAL = max(0, int(_env_float("DEX_MATURE_QUALITY_MIN_SCORE_TOTAL", 0.0)))
_PUMP_EARLY_QUALITY_MIN_AGE_MIN = max(0.0, _env_float("PUMP_EARLY_QUALITY_MIN_AGE_MIN", 0.0))
_PUMP_EARLY_QUALITY_MIN_LIQUIDITY_USD = max(0.0, _env_float("PUMP_EARLY_QUALITY_MIN_LIQUIDITY_USD", 0.0))
_PUMP_EARLY_QUALITY_MIN_VOLUME_USD_24H = max(0.0, _env_float("PUMP_EARLY_QUALITY_MIN_VOLUME_USD_24H", 0.0))
_PUMP_EARLY_QUALITY_MIN_MARKET_CAP_USD = max(0.0, _env_float("PUMP_EARLY_QUALITY_MIN_MARKET_CAP_USD", 0.0))
_PUMP_EARLY_QUALITY_MIN_HOLDERS = max(0, int(_env_float("PUMP_EARLY_QUALITY_MIN_HOLDERS", 0.0)))
_PUMP_EARLY_QUALITY_MIN_SCORE_TOTAL = max(0, int(_env_float("PUMP_EARLY_QUALITY_MIN_SCORE_TOTAL", 0.0)))
_PUMP_EARLY_QUALITY_MAX_PRICE_IMPACT_PCT = max(0.0, _env_float("PUMP_EARLY_QUALITY_MAX_PRICE_IMPACT_PCT", 0.0))
_PUMP_EARLY_LIVE_HARD_MIN_AGE_MIN = max(0.0, _env_float("PUMP_EARLY_LIVE_HARD_MIN_AGE_MIN", 5.0))
_PUMP_EARLY_LIVE_HARD_MIN_LIQUIDITY_USD = max(0.0, _env_float("PUMP_EARLY_LIVE_HARD_MIN_LIQUIDITY_USD", 10_000.0))
_PUMP_EARLY_LIVE_HARD_MIN_SCORE_TOTAL = max(0, int(_env_float("PUMP_EARLY_LIVE_HARD_MIN_SCORE_TOTAL", 45.0)))
_PUMP_EARLY_LIVE_HARD_MIN_VOLUME_USD_24H = max(0.0, _env_float("PUMP_EARLY_LIVE_HARD_MIN_VOLUME_USD_24H", 0.0))
_PUMP_EARLY_LIVE_HARD_MAX_MARKET_CAP_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_LIVE_HARD_MAX_MARKET_CAP_USD", 125_000.0) or 125_000.0),
)
_PUMP_EARLY_LIVE_HARD_MAX_PRICE_IMPACT_PCT = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_LIVE_HARD_MAX_PRICE_IMPACT_PCT", 10.0) or 10.0),
)
_PUMP_EARLY_LIVE_MAX_SNAPSHOT_MISSING_FIELDS = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_LIVE_MAX_SNAPSHOT_MISSING_FIELDS", 3) or 3),
)
_PUMP_EARLY_LIVE_MIN_AGE_EFFECTIVE = max(8.0, _PUMP_EARLY_LIVE_HARD_MIN_AGE_MIN)
_PUMP_EARLY_LIVE_MIN_LIQUIDITY_EFFECTIVE = max(10_000.0, _PUMP_EARLY_LIVE_HARD_MIN_LIQUIDITY_USD)
_PUMP_EARLY_LIVE_MIN_SCORE_EFFECTIVE = max(50.0, float(_PUMP_EARLY_LIVE_HARD_MIN_SCORE_TOTAL))
_PUMP_EARLY_LIVE_MIN_MARKET_CAP_EFFECTIVE = max(20_000.0, _PUMP_EARLY_QUALITY_MIN_MARKET_CAP_USD)
_PAPER_COLD_START_ENABLED = bool(getattr(CFG, "PAPER_COLD_START_ENABLED", True))
_PAPER_COLD_START_MAX_CLOSED_TRADES = max(
    0,
    int(getattr(CFG, "PAPER_COLD_START_MAX_CLOSED_TRADES", 50) or 50),
)
_PAPER_COLD_START_MIN_AGE_MIN = max(
    0.0,
    float(getattr(CFG, "PAPER_COLD_START_MIN_AGE_MIN", 12.0) or 12.0),
)
_PAPER_COLD_START_MIN_SCORE_TOTAL = max(
    0.0,
    float(getattr(CFG, "PAPER_COLD_START_MIN_SCORE_TOTAL", 45) or 45),
)
_PAPER_COLD_START_MIN_LIQUIDITY_USD = max(
    0.0,
    float(getattr(CFG, "PAPER_COLD_START_MIN_LIQUIDITY_USD", 10_000.0) or 10_000.0),
)
_PAPER_COLD_START_MIN_MARKET_CAP_USD = max(
    0.0,
    float(getattr(CFG, "PAPER_COLD_START_MIN_MARKET_CAP_USD", 15_000.0) or 15_000.0),
)
_PAPER_COLD_START_MAX_SNAPSHOT_MISSING_FIELDS = max(
    0,
    int(getattr(CFG, "PAPER_COLD_START_MAX_SNAPSHOT_MISSING_FIELDS", 4) or 4),
)
_PAPER_COLD_START_MIN_RANK_SCORE = max(
    0.0,
    float(getattr(CFG, "PAPER_COLD_START_MIN_RANK_SCORE", 12.5) or 12.5),
)
_PAPER_COLD_START_REQUIRE_PRICE_PCT_5M = bool(getattr(CFG, "PAPER_COLD_START_REQUIRE_PRICE_PCT_5M", True))
_PAPER_COLD_START_MIN_PRICE_PCT_5M = float(
    getattr(CFG, "PAPER_COLD_START_MIN_PRICE_PCT_5M", 0.0) or 0.0
)
_PAPER_COLD_START_MAX_PRICE_PCT_5M = max(
    0.0,
    float(getattr(CFG, "PAPER_COLD_START_MAX_PRICE_PCT_5M", 80.0) or 80.0),
)
_PAPER_COLD_START_SHADOW_PROBE_ENABLED = bool(getattr(CFG, "PAPER_COLD_START_SHADOW_PROBE_ENABLED", True))
_PAPER_COLD_START_SHADOW_PROBE_SIZE_MULTIPLIER = max(
    0.0,
    float(getattr(CFG, "PAPER_COLD_START_SHADOW_PROBE_SIZE_MULTIPLIER", 0.10) or 0.10),
)
_PAPER_AGGRESSIVE_TRADING_ENABLED = bool(getattr(CFG, "PAPER_AGGRESSIVE_TRADING_ENABLED", False))
_PAPER_AGGRESSIVE_MIN_AGE_MIN = max(
    0.0,
    float(getattr(CFG, "PAPER_AGGRESSIVE_MIN_AGE_MIN", 0.05) or 0.0),
)
_PAPER_AGGRESSIVE_MIN_LIQUIDITY_USD = max(
    0.0,
    float(getattr(CFG, "PAPER_AGGRESSIVE_MIN_LIQUIDITY_USD", 1_500.0) or 1_500.0),
)
_PAPER_AGGRESSIVE_MIN_MARKET_CAP_USD = max(
    0.0,
    float(getattr(CFG, "PAPER_AGGRESSIVE_MIN_MARKET_CAP_USD", 2_000.0) or 2_000.0),
)
_PAPER_AGGRESSIVE_MAX_MARKET_CAP_USD = max(
    0.0,
    float(getattr(CFG, "PAPER_AGGRESSIVE_MAX_MARKET_CAP_USD", 500_000.0) or 500_000.0),
)
_PAPER_AGGRESSIVE_MIN_SCORE_TOTAL = max(
    0,
    int(getattr(CFG, "PAPER_AGGRESSIVE_MIN_SCORE_TOTAL", 30) or 30),
)
_PAPER_AGGRESSIVE_MIN_RANK_SCORE = max(
    0.0,
    float(getattr(CFG, "PAPER_AGGRESSIVE_MIN_RANK_SCORE", 35.0) or 35.0),
)
_PAPER_AGGRESSIVE_MIN_TXNS_5M = max(
    0,
    int(getattr(CFG, "PAPER_AGGRESSIVE_MIN_TXNS_5M", 3) or 3),
)
_PAPER_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS = max(
    0,
    int(getattr(CFG, "PAPER_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS", 5) or 5),
)
_PAPER_AGGRESSIVE_MAX_PRICE_IMPACT_PCT = max(
    0.0,
    float(getattr(CFG, "PAPER_AGGRESSIVE_MAX_PRICE_IMPACT_PCT", 20.0) or 20.0),
)
_PAPER_AGGRESSIVE_REQUIRE_ROUTE = bool(getattr(CFG, "PAPER_AGGRESSIVE_REQUIRE_ROUTE", True))
_PAPER_AGGRESSIVE_REQUIRE_PRICE = bool(getattr(CFG, "PAPER_AGGRESSIVE_REQUIRE_PRICE", True))
_PAPER_AGGRESSIVE_BUY_RESEARCH_LANES = bool(getattr(CFG, "PAPER_AGGRESSIVE_BUY_RESEARCH_LANES", True))
_LIVE_AGGRESSIVE_TRADING_ENABLED = bool(getattr(CFG, "LIVE_AGGRESSIVE_TRADING_ENABLED", False))
_LIVE_AGGRESSIVE_MIN_AGE_MIN = max(
    0.0,
    float(getattr(CFG, "LIVE_AGGRESSIVE_MIN_AGE_MIN", 0.05) or 0.0),
)
_LIVE_AGGRESSIVE_MIN_LIQUIDITY_USD = max(
    0.0,
    float(getattr(CFG, "LIVE_AGGRESSIVE_MIN_LIQUIDITY_USD", 1_500.0) or 1_500.0),
)
_LIVE_AGGRESSIVE_MIN_MARKET_CAP_USD = max(
    0.0,
    float(getattr(CFG, "LIVE_AGGRESSIVE_MIN_MARKET_CAP_USD", 2_000.0) or 2_000.0),
)
_LIVE_AGGRESSIVE_MAX_MARKET_CAP_USD = max(
    0.0,
    float(getattr(CFG, "LIVE_AGGRESSIVE_MAX_MARKET_CAP_USD", 500_000.0) or 500_000.0),
)
_LIVE_AGGRESSIVE_MIN_SCORE_TOTAL = max(
    0,
    int(getattr(CFG, "LIVE_AGGRESSIVE_MIN_SCORE_TOTAL", 30) or 30),
)
_LIVE_AGGRESSIVE_MIN_RANK_SCORE = max(
    0.0,
    float(getattr(CFG, "LIVE_AGGRESSIVE_MIN_RANK_SCORE", 35.0) or 35.0),
)
_LIVE_AGGRESSIVE_MIN_TXNS_5M = max(
    0,
    int(getattr(CFG, "LIVE_AGGRESSIVE_MIN_TXNS_5M", 3) or 3),
)
_LIVE_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS = max(
    0,
    int(getattr(CFG, "LIVE_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS", 5) or 5),
)
_LIVE_AGGRESSIVE_MAX_PRICE_IMPACT_PCT = max(
    0.0,
    float(getattr(CFG, "LIVE_AGGRESSIVE_MAX_PRICE_IMPACT_PCT", 20.0) or 20.0),
)
_LIVE_AGGRESSIVE_REQUIRE_ROUTE = bool(getattr(CFG, "LIVE_AGGRESSIVE_REQUIRE_ROUTE", True))
_LIVE_AGGRESSIVE_REQUIRE_PRICE = bool(getattr(CFG, "LIVE_AGGRESSIVE_REQUIRE_PRICE", True))
_LIVE_AGGRESSIVE_BUY_RESEARCH_LANES = bool(getattr(CFG, "LIVE_AGGRESSIVE_BUY_RESEARCH_LANES", True))
_PUMP_EARLY_SNIPER_ENABLED = bool(getattr(CFG, "PUMP_EARLY_SNIPER_ENABLED", True))
_PUMP_EARLY_SNIPER_MODE = str(getattr(CFG, "PUMP_EARLY_SNIPER_MODE", "canary_aggressive") or "canary_aggressive").strip().lower()
_PUMP_EARLY_SNIPER_MIN_AGE_MIN = max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_AGE_MIN", 3.0) or 3.0))
_PUMP_EARLY_SNIPER_MAX_AGE_MIN = max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_AGE_MIN", 30.0) or 30.0))
_PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD = max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD", 2_000.0) or 2_000.0))
_PUMP_EARLY_SNIPER_MICRO_MIN_LIQUIDITY_USD = max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MIN_LIQUIDITY_USD", 1_000.0) or 1_000.0))
_PUMP_EARLY_SNIPER_MICRO_MIN_VOLUME_USD_24H = max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MIN_VOLUME_USD_24H", 30_000.0) or 30_000.0))
_PUMP_EARLY_SNIPER_MIN_MARKET_CAP_USD = max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_MARKET_CAP_USD", 3_000.0) or 3_000.0))
_PUMP_EARLY_SNIPER_MAX_MARKET_CAP_USD = max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_MARKET_CAP_USD", 200_000.0) or 200_000.0))
_PUMP_EARLY_SNIPER_MICRO_MAX_MARKET_CAP_USD = max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MAX_MARKET_CAP_USD", 125_000.0) or 125_000.0))
_PUMP_EARLY_SNIPER_MIN_SCORE_TOTAL = max(0, int(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_SCORE_TOTAL", 35) or 35))
_PUMP_EARLY_SNIPER_MICRO_MIN_SCORE_TOTAL = max(0, int(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MIN_SCORE_TOTAL", 30) or 30))
_PUMP_EARLY_SNIPER_MIN_RANK_SCORE = max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_RANK_SCORE", 42.0) or 42.0))
_PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE = max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE", 45.0) or 45.0))
_PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT = max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT", 15.0) or 15.0))
_PUMP_EARLY_SNIPER_MICRO_MAX_PRICE_IMPACT_PCT = max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MAX_PRICE_IMPACT_PCT", 12.0) or 12.0))
_PUMP_EARLY_SNIPER_MIN_TXNS_5M = max(0, int(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_TXNS_5M", 25) or 25))
_PUMP_EARLY_SNIPER_MICRO_MIN_TXNS_5M = max(0, int(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MIN_TXNS_5M", 80) or 80))
_PUMP_EARLY_SNIPER_MIN_PRICE_PCT_5M = float(getattr(CFG, "PUMP_EARLY_SNIPER_MIN_PRICE_PCT_5M", -12.0) or -12.0)
_PUMP_EARLY_SNIPER_MAX_PRICE_PCT_5M = float(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_PRICE_PCT_5M", 180.0) or 180.0)
_PUMP_EARLY_SNIPER_MICRO_MIN_PRICE_PCT_5M = float(getattr(CFG, "PUMP_EARLY_SNIPER_MICRO_MIN_PRICE_PCT_5M", 8.0) or 8.0)
_PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS = max(0, int(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS", 4) or 4))
_PUMP_EARLY_SNIPER_HOT_MIN_RANK_SCORE = max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_HOT_MIN_RANK_SCORE", 50.0) or 50.0))
_PUMP_EARLY_SNIPER_HOT_MIN_TXNS_5M = max(0, int(getattr(CFG, "PUMP_EARLY_SNIPER_HOT_MIN_TXNS_5M", 100) or 100))
_PUMP_EARLY_SNIPER_HOT_MIN_PRICE_PCT_5M = float(getattr(CFG, "PUMP_EARLY_SNIPER_HOT_MIN_PRICE_PCT_5M", 10.0) or 10.0)
_PUMP_EARLY_SNIPER_HOT_MAX_PRICE_PCT_5M = float(getattr(CFG, "PUMP_EARLY_SNIPER_HOT_MAX_PRICE_PCT_5M", 120.0) or 120.0)
_PUMP_EARLY_SNIPER_HOT_MAX_SNAPSHOT_MISSING_FIELDS = max(0, int(getattr(CFG, "PUMP_EARLY_SNIPER_HOT_MAX_SNAPSHOT_MISSING_FIELDS", 2) or 2))
_PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_ENABLED = bool(
    getattr(CFG, "PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_ENABLED", True)
)
_PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_MIN_AGE_MIN = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_MIN_AGE_MIN", 3.0) or 3.0),
)
_PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_USD", 1_500.0) or 1_500.0),
)
_PUMP_EARLY_PROFIT_LANE_ENABLED = bool(getattr(CFG, "PUMP_EARLY_PROFIT_LANE_ENABLED", True))
_PUMP_EARLY_PROFIT_DEX_ALLOWLIST = {
    item.strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    for item in str(getattr(CFG, "PUMP_EARLY_PROFIT_DEX_ALLOWLIST", "pumpswap") or "pumpswap").split(",")
    if item.strip()
}
_PUMP_EARLY_PROFIT_REQUIRE_REAL_LIQUIDITY = bool(
    getattr(CFG, "PUMP_EARLY_PROFIT_REQUIRE_REAL_LIQUIDITY", True)
)
_PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD", 5_000.0) or 5_000.0),
)
_PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL", 35) or 35),
)
_PUMP_EARLY_PROFIT_MIN_AGE_MIN = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_MIN_AGE_MIN", 3.0) or 3.0),
)
_PUMP_EARLY_PROFIT_MAX_AGE_MIN = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_AGE_MIN", 30.0) or 30.0),
)
_PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT", 10.0) or 10.0),
)
_PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD", 0.0) or 0.0),
)
_PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD", 0.0) or 0.0),
)


def _parse_float_ranges(raw: object) -> tuple[tuple[float, float], ...]:
    ranges: list[tuple[float, float]] = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        lo_raw, hi_raw = item.split(":", 1)
        try:
            lo = float(lo_raw)
            hi = float(hi_raw)
        except Exception:
            continue
        ranges.append((min(lo, hi), max(lo, hi)))
    return tuple(ranges)


_PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES = _parse_float_ranges(
    getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES", "25:999")
)
_PUMP_EARLY_AGGRESSIVE_RESEARCH_GUARD_ENABLED = bool(
    getattr(CFG, "PUMP_EARLY_AGGRESSIVE_RESEARCH_GUARD_ENABLED", True)
)
_PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PRICE5M_RANGES = _parse_float_ranges(
    getattr(CFG, "PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PRICE5M_RANGES", "25:999")
)
_PUMP_EARLY_AGGRESSIVE_RESEARCH_DEX_ALLOWLIST = {
    item.strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    for item in str(getattr(CFG, "PUMP_EARLY_AGGRESSIVE_RESEARCH_DEX_ALLOWLIST", "pumpswap") or "pumpswap").split(",")
    if item.strip()
}
_PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_HIGH_MCAP_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_HIGH_MCAP_USD", 100_000.0) or 100_000.0),
)
_PUMP_EARLY_AGGRESSIVE_RESEARCH_HIGH_MCAP_ALLOW_MIN_TXNS_5M = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_AGGRESSIVE_RESEARCH_HIGH_MCAP_ALLOW_MIN_TXNS_5M", 1_200) or 1_200),
)
_PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PROXY = bool(
    getattr(CFG, "PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PROXY", True)
)
_PUMP_EARLY_METEOR_PRIME_ENABLED = bool(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_ENABLED", False))
_PUMP_EARLY_METEOR_PRIME_MIN_LIQUIDITY_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_LIQUIDITY_USD", 4_000.0) or 4_000.0),
)
_PUMP_EARLY_METEOR_PRIME_MAX_LIQUIDITY_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_LIQUIDITY_USD", 30_000.0) or 30_000.0),
)
_PUMP_EARLY_METEOR_PRIME_MIN_MARKET_CAP_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_MARKET_CAP_USD", 5_000.0) or 5_000.0),
)
_PUMP_EARLY_METEOR_PRIME_MAX_MARKET_CAP_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_MARKET_CAP_USD", 30_000.0) or 30_000.0),
)
_PUMP_EARLY_METEOR_PRIME_MIN_PRICE_PCT_5M = float(
    getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_PRICE_PCT_5M", 110.0) or 110.0
)
_PUMP_EARLY_METEOR_PRIME_MAX_PRICE_PCT_5M = float(
    getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_PCT_5M", 300.0) or 300.0
)
_PUMP_EARLY_METEOR_PRIME_MIN_TXNS_5M = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_TXNS_5M", 220) or 220),
)
_PUMP_EARLY_METEOR_PRIME_MIN_SCORE_TOTAL = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_SCORE_TOTAL", 30) or 30),
)
_PUMP_EARLY_METEOR_PRIME_MIN_AGE_MIN = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_AGE_MIN", 3.0) or 3.0),
)
_PUMP_EARLY_METEOR_PRIME_MAX_AGE_MIN = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_AGE_MIN", 18.0) or 18.0),
)
_PUMP_EARLY_METEOR_PRIME_MAX_PRICE_IMPACT_PCT = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_IMPACT_PCT", 12.0) or 12.0),
)
_PUMP_EARLY_METEOR_PRIME_MIN_VOLUME_USD_24H = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_VOLUME_USD_24H", 8_000.0) or 8_000.0),
)
_PUMP_EARLY_BREAKOUT_PROBE_ENABLED = bool(getattr(CFG, "PUMP_EARLY_BREAKOUT_PROBE_ENABLED", True))
_PUMP_EARLY_BREAKOUT_MIN_LIQUIDITY_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_LIQUIDITY_USD", 5_000.0) or 5_000.0),
)
_PUMP_EARLY_BREAKOUT_MAX_LIQUIDITY_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_LIQUIDITY_USD", 30_000.0) or 30_000.0),
)
_PUMP_EARLY_BREAKOUT_MIN_MARKET_CAP_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_MARKET_CAP_USD", 5_000.0) or 5_000.0),
)
_PUMP_EARLY_BREAKOUT_MAX_MARKET_CAP_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_MARKET_CAP_USD", 60_000.0) or 60_000.0),
)
_PUMP_EARLY_BREAKOUT_MIN_PRICE_PCT_5M = float(
    getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_PRICE_PCT_5M", 25.0) or 25.0
)
_PUMP_EARLY_BREAKOUT_MAX_PRICE_PCT_5M = float(
    getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_PRICE_PCT_5M", 120.0) or 120.0
)
_PUMP_EARLY_BREAKOUT_MIN_TXNS_5M = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_TXNS_5M", 300) or 300),
)
_PUMP_EARLY_BREAKOUT_MIN_VOLUME_USD_24H = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_VOLUME_USD_24H", 20_000.0) or 20_000.0),
)
_PUMP_EARLY_BREAKOUT_MIN_SCORE_TOTAL = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_SCORE_TOTAL", 35) or 35),
)
_PUMP_EARLY_BREAKOUT_MIN_RANK_SCORE = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_RANK_SCORE", 50.0) or 50.0),
)
_PUMP_EARLY_BREAKOUT_MIN_AGE_MIN = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_AGE_MIN", 2.0) or 2.0),
)
_PUMP_EARLY_BREAKOUT_MAX_AGE_MIN = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_AGE_MIN", 15.0) or 15.0),
)
_PUMP_EARLY_BREAKOUT_MAX_PRICE_IMPACT_PCT = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_PRICE_IMPACT_PCT", 8.0) or 8.0),
)
_PUMP_EARLY_BREAKOUT_MAX_OPEN_PAPER = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_OPEN_PAPER", 1) or 1),
)
_PUMP_EARLY_BREAKOUT_MAX_OPEN_LIVE_CANARY = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_OPEN_LIVE_CANARY", 1) or 1),
)
_PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED = bool(
    getattr(CFG, "PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED", True)
)
_PUMP_EARLY_PROFIT_HEALTH_REBASE_CURRENT_GATE = bool(
    getattr(CFG, "PUMP_EARLY_PROFIT_HEALTH_REBASE_CURRENT_GATE", True)
)
_PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD", 25_000.0) or 25_000.0),
)
_PUMP_EARLY_PROFIT_DEEP_NEG_PRICE5M_PCT = float(
    getattr(CFG, "PUMP_EARLY_PROFIT_DEEP_NEG_PRICE5M_PCT", -40.0)
)
_PUMP_EARLY_PROFIT_DEEP_NEG_MIN_TXNS_5M = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_TXNS_5M", 1_500) or 1_500),
)
_PUMP_EARLY_PROFIT_DEEP_NEG_MIN_VOLUME_USD_24H = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_VOLUME_USD_24H", 150_000.0) or 150_000.0),
)
_PUMP_EARLY_PROFIT_EXTREME_PRICE5M_PCT = float(
    getattr(CFG, "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_PCT", 300.0) or 300.0
)
_PUMP_EARLY_PROFIT_EXTREME_PRICE5M_MIN_MCAP_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_MIN_MCAP_USD", 100_000.0) or 100_000.0),
)
_PUMP_EARLY_PROFIT_DEAD_VOLUME_MIN_USD_24H = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_DEAD_VOLUME_MIN_USD_24H", 15_000.0) or 15_000.0),
)
_PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_USD_24H = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_USD_24H", 30_000.0) or 30_000.0),
)
_PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_TXNS_5M = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_TXNS_5M", 1_000) or 1_000),
)
_PUMP_EARLY_PROFIT_HOT_PRICE5M_MIN_PCT = float(
    getattr(CFG, "PUMP_EARLY_PROFIT_HOT_PRICE5M_MIN_PCT", 100.0) or 100.0
)
_PUMP_EARLY_PROFIT_HOT_PRICE5M_MAX_PCT = float(
    getattr(CFG, "PUMP_EARLY_PROFIT_HOT_PRICE5M_MAX_PCT", 180.0) or 180.0
)
_PUMP_EARLY_PROFIT_HOT_MCAP_MIN_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MCAP_MIN_USD", 50_000.0) or 50_000.0),
)
_PUMP_EARLY_PROFIT_HOT_MIN_LIQUIDITY_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MIN_LIQUIDITY_USD", 20_000.0) or 20_000.0),
)
_PUMP_EARLY_PROFIT_HOT_MIN_TXNS_5M = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MIN_TXNS_5M", 600) or 600),
)
_PUMP_EARLY_PROFIT_HOT_MIN_VOLUME_USD_24H = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MIN_VOLUME_USD_24H", 50_000.0) or 50_000.0),
)
_PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H", 15_000.0) or 15_000.0),
)
_PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_TXNS_5M = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_TXNS_5M", 500) or 500),
)
_PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_PRICE5M_PCT = float(
    getattr(CFG, "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_PRICE5M_PCT", 50.0) or 50.0
)
_PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_TXNS_5M = max(
    0,
    int(getattr(CFG, "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_TXNS_5M", 350) or 350),
)
_PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_VOLUME_USD_24H = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_VOLUME_USD_24H", 100_000.0) or 100_000.0),
)
_PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT = float(
    getattr(CFG, "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT", 40.0) or 40.0
)
_PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT = float(
    getattr(CFG, "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT", 50.0) or 50.0
)
_PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD = max(
    0.0,
    float(getattr(CFG, "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD", 100_000.0) or 100_000.0),
)

_policy_reject_seen: dict[str, float] = {}
_stream_candidate_cooldown_until: dict[str, float] = {}


def _sample_address(sample: dict | object) -> str:
    try:
        addr = getattr(sample, "get")("address")  # type: ignore[misc]
    except Exception:
        addr = None
    return str(addr or "").strip()


def _prune_expiring(cache: dict[str, float], *, now: float | None = None) -> None:
    if not cache:
        return
    ts = time.monotonic() if now is None else float(now)
    stale = [key for key, until in cache.items() if until <= ts]
    for key in stale:
        cache.pop(key, None)


def _remember_stream_candidate_cooldown(addr: str, ttl_s: int) -> None:
    if ttl_s <= 0 or not addr:
        return
    now = time.monotonic()
    _prune_expiring(_stream_candidate_cooldown_until, now=now)
    _stream_candidate_cooldown_until[addr] = max(
        _stream_candidate_cooldown_until.get(addr, 0.0),
        now + float(ttl_s),
    )


def _stream_candidate_is_cooled(addr: str) -> bool:
    if not addr:
        return False
    now = time.monotonic()
    _prune_expiring(_stream_candidate_cooldown_until, now=now)
    return _stream_candidate_cooldown_until.get(addr, 0.0) > now


def _stream_candidate_cooldown_s(token: dict, reason: str) -> int:
    discovered_via = str(token.get("discovered_via") or "").strip().lower()
    if discovered_via != "pumpfun":
        return 0
    if reason == "entry_quality":
        return _PUMP_EARLY_QUALITY_BACKOFF_S
    if reason in {"no_liq", "incomplete", "dex_nil"}:
        return _PUMPFUN_STREAM_COOLDOWN_NO_LIQ_S
    return _PUMPFUN_STREAM_COOLDOWN_REJECT_S


def _requeue_or_cooldown_candidate(addr: str, token: dict, *, reason: str, backoff: int) -> bool:
    if str(token.get("discovered_via") or "").strip().lower() == "pumpfun":
        _remember_stream_candidate_cooldown(addr, _stream_candidate_cooldown_s(token, reason))
        return False
    return _ensure_requeue_with_stats(addr, reason=reason, backoff=backoff, token=token)


def _metric_float(token: dict, *keys: str) -> float:
    for key in keys:
        raw = token.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
            if value != value or value == float("inf") or value == float("-inf"):
                continue
            return value
        except Exception:
            continue
    return 0.0


def _metric_optional_float(token: dict, *keys: str) -> float | None:
    for key in keys:
        raw = token.get(key)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
            if value != value or value == float("inf") or value == float("-inf"):
                continue
            return value
        except Exception:
            continue
    return None


def _metric_int(token: dict, *keys: str) -> int:
    try:
        return int(_metric_float(token, *keys))
    except Exception:
        return 0


def _candidate_age_minutes(token: dict) -> float:
    age_min = _metric_float(token, "age_min", "age_minutes")
    if age_min > 0:
        return age_min

    created_raw = token.get("created_at") or token.get("createdAt")
    created_dt: dt.datetime | None = None
    if isinstance(created_raw, dt.datetime):
        created_dt = created_raw if created_raw.tzinfo else created_raw.replace(tzinfo=dt.timezone.utc)
    elif isinstance(created_raw, str):
        created_dt = parse_iso_utc(created_raw)

    if created_dt is None:
        return 0.0
    return max(0.0, (utc_now() - created_dt).total_seconds() / 60.0)


def _paper_cold_start_active(closed_trades: int | None = None) -> bool:
    if not DRY_RUN or not _PAPER_COLD_START_ENABLED:
        return False
    if closed_trades is None:
        closed_trades = int(_stats.get("sold", 0) or 0)
    return int(closed_trades or 0) < int(_PAPER_COLD_START_MAX_CLOSED_TRADES)


def _paper_cold_start_shadow_probe_allowed(
    strategy_decision: object,
    size_decision: object,
    quality_ok: bool,
    closed_trades: int | None,
) -> bool:
    if not bool(_PAPER_COLD_START_SHADOW_PROBE_ENABLED):
        return False
    if not bool(quality_ok):
        return False
    if not _paper_cold_start_active(closed_trades):
        return False
    if str(getattr(size_decision, "regime", "") or "").strip().lower() != "pump_early":
        return False
    if str(getattr(strategy_decision, "requested_mode", "") or "").strip().lower() != "live":
        return False
    if str(getattr(strategy_decision, "action", "") or "").strip().lower() != "shadow":
        return False
    reason = str(getattr(strategy_decision, "reason", "") or "").strip().lower()
    reason_parts = {part.strip() for part in reason.split(",") if part.strip()}
    return bool(reason_parts & {"loss_streak", "recovery_not_ready"})


def _add_min_failure(failures: list[str], name: str, value: float, threshold: float) -> None:
    if threshold != 0 and value < threshold:
        failures.append(f"{name}<{threshold:g}")


def _add_max_failure(failures: list[str], name: str, value: float, threshold: float) -> None:
    if threshold > 0 and value > threshold:
        failures.append(f"{name}>{threshold:g}")


def _sniper_rank_score(rank_info: dict[str, object] | None) -> float:
    try:
        return float((rank_info or {}).get("rank_score") or 0.0)
    except Exception:
        return 0.0


def _evaluate_sniper_core(token: dict, rank_score: float) -> list[str]:
    failures: list[str] = []
    route_required = bool(token.get("require_jupiter_for_buy", True))
    has_route = bool(_metric_int(token, "has_jupiter_route"))
    if route_required and not has_route:
        failures.append("route_required")

    price_pct_5m = _metric_optional_float(token, "price_pct_5m")
    if price_pct_5m is None:
        failures.append("price5m_missing")
    else:
        _add_min_failure(failures, "price5m", price_pct_5m, _PUMP_EARLY_SNIPER_MIN_PRICE_PCT_5M)
        _add_max_failure(failures, "price5m", price_pct_5m, _PUMP_EARLY_SNIPER_MAX_PRICE_PCT_5M)

    _add_min_failure(failures, "age", _candidate_age_minutes(token), _PUMP_EARLY_SNIPER_MIN_AGE_MIN)
    _add_max_failure(failures, "age", _candidate_age_minutes(token), _PUMP_EARLY_SNIPER_MAX_AGE_MIN)
    _add_min_failure(failures, "liq", _metric_float(token, "liquidity_usd"), _PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD)
    _add_min_failure(failures, "mcap", _metric_float(token, "market_cap_usd"), _PUMP_EARLY_SNIPER_MIN_MARKET_CAP_USD)
    _add_max_failure(failures, "mcap", _metric_float(token, "market_cap_usd"), _PUMP_EARLY_SNIPER_MAX_MARKET_CAP_USD)
    _add_min_failure(failures, "score", float(_metric_int(token, "score_total")), float(_PUMP_EARLY_SNIPER_MIN_SCORE_TOTAL))
    _add_min_failure(failures, "rank", rank_score, _PUMP_EARLY_SNIPER_MIN_RANK_SCORE)
    _add_min_failure(failures, "txns5m", float(_metric_int(token, "txns_last_5m")), float(_PUMP_EARLY_SNIPER_MIN_TXNS_5M))
    _add_max_failure(
        failures,
        "impact",
        max(0.0, _metric_float(token, "price_impact_pct")),
        _PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT,
    )
    snapshot_missing = max(0, _metric_int(token, "snapshot_missing_fields"))
    if snapshot_missing > _PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS:
        failures.append(f"missing>{_PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS}")
    return failures


def _evaluate_sniper_micro(token: dict, rank_score: float) -> list[str]:
    failures: list[str] = []
    route_required = bool(token.get("require_jupiter_for_buy", True))
    has_route = bool(_metric_int(token, "has_jupiter_route"))
    if route_required and not has_route:
        failures.append("route_required")

    price_pct_5m = _metric_optional_float(token, "price_pct_5m")
    if price_pct_5m is None:
        failures.append("price5m_missing")
    else:
        _add_min_failure(failures, "price5m", price_pct_5m, _PUMP_EARLY_SNIPER_MICRO_MIN_PRICE_PCT_5M)

    _add_min_failure(failures, "age", _candidate_age_minutes(token), _PUMP_EARLY_SNIPER_MIN_AGE_MIN)
    _add_max_failure(failures, "age", _candidate_age_minutes(token), _PUMP_EARLY_SNIPER_MAX_AGE_MIN)
    _add_min_failure(
        failures,
        "liq",
        _metric_float(token, "liquidity_usd"),
        _PUMP_EARLY_SNIPER_MICRO_MIN_LIQUIDITY_USD,
    )
    _add_max_failure(
        failures,
        "mcap",
        _metric_float(token, "market_cap_usd"),
        _PUMP_EARLY_SNIPER_MICRO_MAX_MARKET_CAP_USD,
    )
    _add_min_failure(
        failures,
        "score",
        float(_metric_int(token, "score_total")),
        float(_PUMP_EARLY_SNIPER_MICRO_MIN_SCORE_TOTAL),
    )
    _add_min_failure(failures, "rank", rank_score, _PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE)
    _add_min_failure(
        failures,
        "txns5m",
        float(_metric_int(token, "txns_last_5m")),
        float(_PUMP_EARLY_SNIPER_MICRO_MIN_TXNS_5M),
    )
    _add_min_failure(
        failures,
        "vol",
        _metric_float(token, "volume_24h_usd"),
        _PUMP_EARLY_SNIPER_MICRO_MIN_VOLUME_USD_24H,
    )
    _add_max_failure(
        failures,
        "impact",
        max(0.0, _metric_float(token, "price_impact_pct")),
        _PUMP_EARLY_SNIPER_MICRO_MAX_PRICE_IMPACT_PCT,
    )
    snapshot_missing = max(0, _metric_int(token, "snapshot_missing_fields"))
    if snapshot_missing > _PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS:
        failures.append(f"missing>{_PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS}")
    return failures


def _sniper_hot_ok(token: dict, rank_score: float) -> bool:
    price_pct_5m = _metric_optional_float(token, "price_pct_5m")
    if price_pct_5m is None:
        return False
    snapshot_missing = max(0, _metric_int(token, "snapshot_missing_fields"))
    return (
        rank_score >= _PUMP_EARLY_SNIPER_HOT_MIN_RANK_SCORE
        and _metric_int(token, "txns_last_5m") >= _PUMP_EARLY_SNIPER_HOT_MIN_TXNS_5M
        and _PUMP_EARLY_SNIPER_HOT_MIN_PRICE_PCT_5M <= price_pct_5m <= _PUMP_EARLY_SNIPER_HOT_MAX_PRICE_PCT_5M
        and snapshot_missing <= _PUMP_EARLY_SNIPER_HOT_MAX_SNAPSHOT_MISSING_FIELDS
    )


def _gate_dex_id(token: dict) -> str:
    raw = token.get("dex_id") or token.get("dexId") or token.get("discovered_via")
    return str(raw or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def _is_liquidity_proxy(token: dict) -> bool:
    return bool(_metric_int(token, "liquidity_usd_is_proxy") or _metric_int(token, "sniper_liquidity_proxy"))


def _mcap_bucket(mcap: float) -> str:
    if mcap <= 0:
        return "missing"
    if mcap < 25_000:
        return "<25k"
    if mcap < 50_000:
        return "25k_50k"
    if mcap < 100_000:
        return "50k_100k"
    if mcap < 200_000:
        return "100k_200k"
    return ">=200k"


def _price5m_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 0:
        return "<0"
    if value < 25:
        return "0_25"
    if value < 50:
        return "25_50"
    if value < 100:
        return "50_100"
    if value < 180:
        return "100_180"
    return ">=180"


def _price5m_blocked_bucket(value: float | None) -> str | None:
    if value is None:
        return "price5m_missing"
    for lo, hi in _PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES:
        if lo <= float(value) <= hi:
            return f"price5m_{lo:g}_{hi:g}"
    return None


def _aggressive_research_guard_failures(token: dict) -> list[str]:
    if not _PUMP_EARLY_AGGRESSIVE_RESEARCH_GUARD_ENABLED:
        return []
    failures: list[str] = []
    price_pct_5m = _metric_optional_float(token, "price_pct_5m")
    mcap = _metric_float(token, "market_cap_usd")
    txns_5m = _metric_int(token, "txns_last_5m")
    dex_id = _gate_dex_id(token)

    if _PUMP_EARLY_AGGRESSIVE_RESEARCH_DEX_ALLOWLIST and dex_id not in _PUMP_EARLY_AGGRESSIVE_RESEARCH_DEX_ALLOWLIST:
        failures.append(f"research_dex!={','.join(sorted(_PUMP_EARLY_AGGRESSIVE_RESEARCH_DEX_ALLOWLIST))}")

    if _PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PROXY and _is_liquidity_proxy(token):
        failures.append("research_liq_proxy")

    if price_pct_5m is None:
        failures.append("research_price5m_missing")
    else:
        for lo, hi in _PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PRICE5M_RANGES:
            if lo <= float(price_pct_5m) <= hi:
                failures.append(f"research_price5m_{lo:g}_{hi:g}")
                break

    high_mcap_block = _PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_HIGH_MCAP_USD
    if high_mcap_block > 0 and mcap >= high_mcap_block:
        high_support_momentum = (
            price_pct_5m is not None
            and 25.0 <= float(price_pct_5m) < 50.0
            and txns_5m >= _PUMP_EARLY_AGGRESSIVE_RESEARCH_HIGH_MCAP_ALLOW_MIN_TXNS_5M
        )
        if not high_support_momentum:
            failures.append(f"research_mcap>={high_mcap_block:g}")
    return failures


def _meteor_prime_failures(token: dict) -> list[str]:
    failures: list[str] = []
    price_pct_5m = _metric_optional_float(token, "price_pct_5m")
    txns_5m = float(_metric_int(token, "txns_last_5m"))
    liquidity = _metric_float(token, "liquidity_usd")
    mcap = _metric_float(token, "market_cap_usd")
    volume_24h = max(_metric_float(token, "volume_24h_usd"), _metric_float(token, "volume_usd_24h"))
    impact = max(0.0, _metric_float(token, "price_impact_pct"))

    if _gate_dex_id(token) != "pumpswap":
        failures.append("meteor_dex_not_pumpswap")
    if not bool(_metric_int(token, "has_jupiter_route")):
        failures.append("meteor_route_required")
    if _is_liquidity_proxy(token):
        failures.append("meteor_liq_proxy")
    _add_min_failure(failures, "meteor_liq", liquidity, _PUMP_EARLY_METEOR_PRIME_MIN_LIQUIDITY_USD)
    _add_max_failure(failures, "meteor_liq", liquidity, _PUMP_EARLY_METEOR_PRIME_MAX_LIQUIDITY_USD)
    _add_min_failure(failures, "meteor_mcap", mcap, _PUMP_EARLY_METEOR_PRIME_MIN_MARKET_CAP_USD)
    _add_max_failure(failures, "meteor_mcap", mcap, _PUMP_EARLY_METEOR_PRIME_MAX_MARKET_CAP_USD)
    if price_pct_5m is None:
        failures.append("meteor_price5m_missing")
    else:
        _add_min_failure(failures, "meteor_price5m", price_pct_5m, _PUMP_EARLY_METEOR_PRIME_MIN_PRICE_PCT_5M)
        _add_max_failure(failures, "meteor_price5m", price_pct_5m, _PUMP_EARLY_METEOR_PRIME_MAX_PRICE_PCT_5M)
    _add_min_failure(failures, "meteor_txns5m", txns_5m, float(_PUMP_EARLY_METEOR_PRIME_MIN_TXNS_5M))
    _add_min_failure(
        failures,
        "meteor_score",
        float(_metric_int(token, "score_total")),
        float(_PUMP_EARLY_METEOR_PRIME_MIN_SCORE_TOTAL),
    )
    _add_min_failure(failures, "meteor_age", _candidate_age_minutes(token), _PUMP_EARLY_METEOR_PRIME_MIN_AGE_MIN)
    _add_max_failure(failures, "meteor_age", _candidate_age_minutes(token), _PUMP_EARLY_METEOR_PRIME_MAX_AGE_MIN)
    _add_max_failure(failures, "meteor_impact", impact, _PUMP_EARLY_METEOR_PRIME_MAX_PRICE_IMPACT_PCT)
    _add_min_failure(failures, "meteor_vol", volume_24h, _PUMP_EARLY_METEOR_PRIME_MIN_VOLUME_USD_24H)
    return failures


def _breakout_probe_failures(token: dict, rank_score: float) -> list[str]:
    failures: list[str] = []
    price_pct_5m = _metric_optional_float(token, "price_pct_5m")
    txns_5m = float(_metric_int(token, "txns_last_5m"))
    liquidity = _metric_float(token, "liquidity_usd")
    mcap = _metric_float(token, "market_cap_usd")
    volume_candidates = [
        value
        for value in (
            _metric_optional_float(token, "volume_24h_usd"),
            _metric_optional_float(token, "volume_usd_24h"),
        )
        if value is not None and value > 0
    ]
    volume_24h = max(volume_candidates) if volume_candidates else None
    impact = max(0.0, _metric_float(token, "price_impact_pct"))
    age_min = _candidate_age_minutes(token)

    if _gate_dex_id(token) != "pumpswap":
        failures.append("breakout_dex_not_pumpswap")
    if not bool(_metric_int(token, "has_jupiter_route")):
        failures.append("breakout_route_required")
    if _is_liquidity_proxy(token):
        failures.append("breakout_liq_proxy")
    if _metric_float(token, "price_usd") <= 0:
        failures.append("breakout_price_missing")
    _add_min_failure(failures, "breakout_liq", liquidity, _PUMP_EARLY_BREAKOUT_MIN_LIQUIDITY_USD)
    _add_max_failure(failures, "breakout_liq", liquidity, _PUMP_EARLY_BREAKOUT_MAX_LIQUIDITY_USD)
    _add_min_failure(failures, "breakout_mcap", mcap, _PUMP_EARLY_BREAKOUT_MIN_MARKET_CAP_USD)
    _add_max_failure(failures, "breakout_mcap", mcap, _PUMP_EARLY_BREAKOUT_MAX_MARKET_CAP_USD)
    if price_pct_5m is None:
        failures.append("breakout_price5m_missing")
    else:
        _add_min_failure(failures, "breakout_price5m", price_pct_5m, _PUMP_EARLY_BREAKOUT_MIN_PRICE_PCT_5M)
        _add_max_failure(failures, "breakout_price5m", price_pct_5m, _PUMP_EARLY_BREAKOUT_MAX_PRICE_PCT_5M)
    _add_min_failure(failures, "breakout_txns5m", txns_5m, float(_PUMP_EARLY_BREAKOUT_MIN_TXNS_5M))
    if volume_24h is None:
        failures.append("breakout_vol_missing")
    else:
        _add_min_failure(failures, "breakout_vol", volume_24h, _PUMP_EARLY_BREAKOUT_MIN_VOLUME_USD_24H)
    _add_min_failure(
        failures,
        "breakout_score",
        float(_metric_int(token, "score_total")),
        float(_PUMP_EARLY_BREAKOUT_MIN_SCORE_TOTAL),
    )
    _add_min_failure(failures, "breakout_rank", rank_score, _PUMP_EARLY_BREAKOUT_MIN_RANK_SCORE)
    _add_min_failure(failures, "breakout_age", age_min, _PUMP_EARLY_BREAKOUT_MIN_AGE_MIN)
    _add_max_failure(failures, "breakout_age", age_min, _PUMP_EARLY_BREAKOUT_MAX_AGE_MIN)
    _add_max_failure(failures, "breakout_impact", impact, _PUMP_EARLY_BREAKOUT_MAX_PRICE_IMPACT_PCT)
    return failures


def _profit_shape_guard_failures(token: dict, *, meteor_prime: bool) -> list[str]:
    if not _PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED or meteor_prime:
        return []
    failures: list[str] = []
    price_pct_5m = _metric_optional_float(token, "price_pct_5m")
    txns_5m = _metric_int(token, "txns_last_5m")
    liquidity = _metric_float(token, "liquidity_usd")
    mcap = _metric_float(token, "market_cap_usd")
    volume_candidates = [
        value
        for value in (
            _metric_optional_float(token, "volume_24h_usd"),
            _metric_optional_float(token, "volume_usd_24h"),
        )
        if value is not None and value > 0
    ]
    volume_24h = max(volume_candidates) if volume_candidates else None

    if _PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD > 0 and mcap >= _PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD:
        failures.append(f"shape_mcap>={_PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD:g}")
    if (
        price_pct_5m is not None
        and price_pct_5m >= _PUMP_EARLY_PROFIT_EXTREME_PRICE5M_PCT
        and mcap >= _PUMP_EARLY_PROFIT_EXTREME_PRICE5M_MIN_MCAP_USD
    ):
        failures.append("shape_extreme_price5m_mcap")
    if (
        price_pct_5m is not None
        and price_pct_5m <= _PUMP_EARLY_PROFIT_DEEP_NEG_PRICE5M_PCT
        and txns_5m < _PUMP_EARLY_PROFIT_DEEP_NEG_MIN_TXNS_5M
        and volume_24h is not None
        and volume_24h < _PUMP_EARLY_PROFIT_DEEP_NEG_MIN_VOLUME_USD_24H
    ):
        failures.append("shape_deep_negative_price5m")
    if (
        volume_24h is not None
        and
        _PUMP_EARLY_PROFIT_DEAD_VOLUME_MIN_USD_24H
        <= volume_24h
        < _PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_USD_24H
        and txns_5m < _PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_TXNS_5M
    ):
        failures.append("shape_dead_volume")
    if (
        price_pct_5m is not None
        and _PUMP_EARLY_PROFIT_HOT_PRICE5M_MIN_PCT <= price_pct_5m <= _PUMP_EARLY_PROFIT_HOT_PRICE5M_MAX_PCT
        and mcap >= _PUMP_EARLY_PROFIT_HOT_MCAP_MIN_USD
        and (
            liquidity < _PUMP_EARLY_PROFIT_HOT_MIN_LIQUIDITY_USD
            or txns_5m < _PUMP_EARLY_PROFIT_HOT_MIN_TXNS_5M
            or (volume_24h is not None and volume_24h < _PUMP_EARLY_PROFIT_HOT_MIN_VOLUME_USD_24H)
        )
    ):
        failures.append("shape_hot_weak_support")
    if (
        volume_24h is not None
        and
        price_pct_5m is not None
        and volume_24h < _PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H
        and txns_5m < _PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_TXNS_5M
        and price_pct_5m < _PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_PRICE5M_PCT
    ):
        failures.append("shape_low_volume_no_momentum")
    if (
        price_pct_5m is not None
        and mcap < 25_000.0
        and 25.0 <= price_pct_5m < 50.0
        and txns_5m < _PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_TXNS_5M
        and volume_24h is not None
        and volume_24h < _PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_VOLUME_USD_24H
    ):
        failures.append("shape_prime_mid_momentum_weak")
    if (
        price_pct_5m is not None
        and mcap >= _PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD
        and _PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT
        <= price_pct_5m
        < _PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT
    ):
        failures.append("shape_high_mcap_mid_momentum")
    return failures


def _set_profit_gate_context(
    token: dict,
    *,
    entry_lane: str,
    gate_profile: str,
    failures: list[str],
    blocked_bucket: str | None,
    rank_score: float,
) -> None:
    price_pct_5m = _metric_optional_float(token, "price_pct_5m")
    mcap = _metric_float(token, "market_cap_usd")
    impact = _metric_float(token, "price_impact_pct")
    dex_id = _gate_dex_id(token)
    token["entry_lane"] = entry_lane
    token["sniper_gate_profile"] = gate_profile
    token["gate_profile"] = gate_profile
    token["live_profit_gate_profile"] = gate_profile
    token["live_profit_gate_failed_count"] = int(len(failures))
    token["live_profit_gate_failures"] = ",".join(failures[:12])
    token["sniper_gate_failures"] = ",".join(failures[:12])
    token["profit_gate_reject_reasons"] = ",".join(failures[:12])
    token["blocked_bucket"] = blocked_bucket
    token["mcap_bucket"] = _mcap_bucket(mcap)
    token["price5m_bucket"] = _price5m_bucket(price_pct_5m)
    token["venue_is_pumpswap"] = int(dex_id == "pumpswap")
    token["liquidity_is_proxy"] = int(_is_liquidity_proxy(token))
    token["liquidity_usd_is_proxy"] = int(_is_liquidity_proxy(token))
    token["impact_zero_flag"] = int(impact == 0.0)
    token["live_rank_gate_threshold"] = 0.0
    token["live_rank_gate_source"] = "pumpswap_profit_policy"
    token["live_rank_gate_margin"] = 0.0
    token["rank_score"] = float(rank_score)


def _evaluate_pumpswap_profit_gate(
    token: dict,
    rank_info: dict[str, object] | None = None,
) -> dict[str, object]:
    rank_score = _sniper_rank_score(rank_info)
    failures: list[str] = []
    blocked_bucket: str | None = None
    dex_id = _gate_dex_id(token)
    has_route = bool(_metric_int(token, "has_jupiter_route"))
    liquidity = _metric_float(token, "liquidity_usd")
    mcap = _metric_float(token, "market_cap_usd")
    price = _metric_float(token, "price_usd")
    price_pct_5m = _metric_optional_float(token, "price_pct_5m")
    impact = max(0.0, _metric_float(token, "price_impact_pct"))
    age_min = _candidate_age_minutes(token)
    score_total = _metric_int(token, "score_total")
    liq_proxy = _is_liquidity_proxy(token)
    meteor_failures = _meteor_prime_failures(token) if _PUMP_EARLY_METEOR_PRIME_ENABLED else ["meteor_disabled"]
    breakout_failures = (
        _breakout_probe_failures(token, rank_score)
        if _PUMP_EARLY_BREAKOUT_PROBE_ENABLED
        else ["breakout_disabled"]
    )
    cfg = globals().get("CFG")
    precision_price_min = float(getattr(cfg, "PUMP_EARLY_PRECISION_MIN_PRICE5M_PCT", -60.0) or -60.0)
    precision_price_max = float(getattr(cfg, "PUMP_EARLY_PRECISION_MAX_PRICE5M_PCT", 50.0) or 50.0)
    precision_min_liq = float(getattr(cfg, "PUMP_EARLY_PRECISION_MIN_LIQUIDITY_USD", 4_500.0) or 4_500.0)
    precision_max_impact = float(getattr(cfg, "PUMP_EARLY_PRECISION_MAX_PRICE_IMPACT_PCT", 10.0) or 10.0)
    precision_high_mcap_min_liq = float(
        getattr(cfg, "PUMP_EARLY_PRECISION_HIGH_MCAP_MIN_LIQUIDITY_USD", 25_000.0) or 25_000.0
    )
    precision_allowed = bool(
        bool(getattr(cfg, "PUMP_EARLY_PRECISION_GATE_ENABLED", False))
        and dex_id == "pumpswap"
        and has_route
        and not liq_proxy
        and price > 0
        and liquidity >= precision_min_liq
        and mcap > 0
        and price_pct_5m is not None
        and precision_price_min <= float(price_pct_5m) < precision_price_max
        and impact <= precision_max_impact
        and not (50_000.0 <= mcap < 100_000.0)
        and not (mcap >= 100_000.0 and liquidity < precision_high_mcap_min_liq and rank_score < 60.0)
        and age_min <= _PUMP_EARLY_PROFIT_MAX_AGE_MIN
    )

    if dex_id not in _PUMP_EARLY_PROFIT_DEX_ALLOWLIST:
        failures.append(f"dex!={','.join(sorted(_PUMP_EARLY_PROFIT_DEX_ALLOWLIST)) or 'pumpswap'}")
        blocked_bucket = blocked_bucket or f"dex_{dex_id or 'missing'}"
    if not has_route:
        failures.append("route_required")
    if _PUMP_EARLY_PROFIT_REQUIRE_REAL_LIQUIDITY and liq_proxy:
        failures.append("liq_proxy")
        blocked_bucket = blocked_bucket or "liquidity_proxy"
    if price <= 0:
        failures.append("price_missing")
    if liquidity <= 0:
        failures.append("liq_missing")
    _add_min_failure(failures, "liq", liquidity, _PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD)
    if mcap <= 0:
        failures.append("mcap_missing")
    if (
        _PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD > 0
        and _PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD > 0
        and _PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD <= mcap <= _PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD
    ):
        failures.append(
            f"mcap_block:{_PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD:g}_{_PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD:g}"
        )
        blocked_bucket = blocked_bucket or "mcap_25k_50k"
    _add_min_failure(failures, "age", age_min, _PUMP_EARLY_PROFIT_MIN_AGE_MIN)
    _add_max_failure(failures, "age", age_min, _PUMP_EARLY_PROFIT_MAX_AGE_MIN)
    _add_min_failure(failures, "score", float(score_total), float(_PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL))
    _add_max_failure(failures, "impact", impact, _PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT)

    price_block = _price5m_blocked_bucket(price_pct_5m)
    if price_block:
        failures.append(price_block)
        blocked_bucket = blocked_bucket or price_block

    meteor_prime = not meteor_failures
    breakout_probe = not breakout_failures
    preliminary_prime = (
        (not failures)
        and mcap < 25_000
        and _PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD <= liquidity <= 25_000
        and not liq_proxy
        and dex_id == "pumpswap"
    )
    shape_failures = _profit_shape_guard_failures(token, meteor_prime=meteor_prime)
    pnl_guard = evaluate_profit_pnl_guard(
        token,
        gate_profile="pumpswap_profit_broad",
        prime=preliminary_prime,
        meteor_prime=meteor_prime,
        breakout_probe=breakout_probe,
    )
    if shape_failures and not blocked_bucket:
        blocked_bucket = shape_failures[0]
    pnl_guard_failures = list(pnl_guard.failures)
    if pnl_guard_failures and not blocked_bucket:
        blocked_bucket = pnl_guard.blocked_bucket
    standard_failures = list(failures)
    effective_failures = [] if precision_allowed else [*standard_failures, *shape_failures, *pnl_guard_failures]
    allowed = precision_allowed or (not effective_failures) or meteor_prime or breakout_probe
    prime = (
        (not effective_failures)
        and mcap < 25_000
        and _PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD <= liquidity <= 25_000
        and not liq_proxy
        and dex_id == "pumpswap"
    )
    gate_profile = (
        "pumpswap_precision"
        if precision_allowed
        else "pumpswap_meteor_prime"
        if meteor_prime
        else "pumpswap_breakout_probe"
        if breakout_probe and effective_failures
        else "pumpswap_profit_prime"
        if prime
        else "pumpswap_profit_broad"
    )
    entry_lane = (
        "pump_early_pumpswap_breakout_probe"
        if allowed and gate_profile == "pumpswap_breakout_probe"
        else "pump_early_pumpswap_profit"
        if allowed
        else "pump_early_sniper_research"
    )
    context_failures = [] if allowed else effective_failures
    _set_profit_gate_context(
        token,
        entry_lane=entry_lane,
        gate_profile=gate_profile if allowed else "pumpswap_profit_research",
        failures=context_failures,
        blocked_bucket=None if allowed else blocked_bucket,
        rank_score=rank_score,
    )
    if precision_allowed:
        token["profit_lane_tier"] = "pump_early_pumpswap_precision"
        token["profit_shape_guard_failures"] = ""
        token["profit_pnl_guard_failures"] = ""
    elif meteor_prime:
        token["profit_lane_tier"] = "pump_early_meteor_prime"
        token["meteor_prime_standard_failures"] = ",".join(standard_failures[:12])
        token["profit_shape_guard_failures"] = ""
        token["profit_pnl_guard_failures"] = ""
    elif gate_profile == "pumpswap_breakout_probe":
        token["profit_lane_tier"] = "pump_early_pumpswap_breakout_probe"
        token["breakout_standard_failures"] = ",".join(effective_failures[:12])
        token["breakout_gate_failures"] = ""
        token["profit_shape_guard_failures"] = ""
        token["profit_pnl_guard_failures"] = ""
    elif prime:
        token["profit_lane_tier"] = "pump_early_pumpswap_prime"
        token["profit_shape_guard_failures"] = ""
        token["profit_pnl_guard_failures"] = ""
    else:
        token["breakout_gate_failures"] = ",".join(breakout_failures[:12])
        token["profit_shape_guard_failures"] = ",".join(shape_failures[:12])
        token["profit_pnl_guard_failures"] = ",".join(pnl_guard_failures[:12])
    return {
        "allowed": bool(allowed),
        "entry_lane": entry_lane,
        "gate_profile": gate_profile if allowed else "pumpswap_profit_research",
        "reject_reasons": list(context_failures),
        "research_eligible": not bool(allowed),
        "blocked_bucket": None if allowed else blocked_bucket,
    }


def _tag_pump_sniper_gate(token: dict, rank_info: dict[str, object] | None = None) -> tuple[bool, str]:
    if str(token.get("entry_lane") or "").strip().lower() == "pump_early_green_candle_sniper":
        token["live_profit_gate_failed_count"] = 0
        token.setdefault("gate_profile", "green_sniper")
        token.setdefault("sniper_gate_profile", "green_sniper")
        return True, ""
    if _PUMP_EARLY_PROFIT_LANE_ENABLED:
        decision = _evaluate_pumpswap_profit_gate(token, rank_info)
        if bool(decision.get("allowed")):
            return True, ""
        failures = [str(item) for item in (decision.get("reject_reasons") or [])]
        profile = str(decision.get("gate_profile") or "pumpswap_profit_research")
        return False, f"live_profit_gate:{profile}:{','.join(failures[:4])}"

    rank_score = _sniper_rank_score(rank_info)
    core_failures = _evaluate_sniper_core(token, rank_score)
    micro_failures = _evaluate_sniper_micro(token, rank_score)

    if not core_failures:
        profile = "sniper_hot" if _sniper_hot_ok(token, rank_score) else "sniper_core"
        token["entry_lane"] = "pump_early_sniper"
        token["sniper_gate_profile"] = profile
        token["live_profit_gate_profile"] = profile
        token["live_rank_gate_threshold"] = _PUMP_EARLY_SNIPER_MIN_RANK_SCORE
        token["live_rank_gate_source"] = "sniper_policy"
        token["live_rank_gate_margin"] = max(0.0, _PUMP_EARLY_SNIPER_MIN_RANK_SCORE - rank_score)
        token["live_profit_gate_failed_count"] = 0
        token["live_profit_gate_failures"] = ""
        token["sniper_gate_failures"] = ""
        return True, ""

    if not micro_failures:
        token["entry_lane"] = "pump_early_sniper"
        token["sniper_gate_profile"] = "sniper_micro"
        token["live_profit_gate_profile"] = "sniper_micro"
        token["live_rank_gate_threshold"] = _PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE
        token["live_rank_gate_source"] = "sniper_policy"
        token["live_rank_gate_margin"] = max(0.0, _PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE - rank_score)
        token["live_profit_gate_failed_count"] = 0
        token["live_profit_gate_failures"] = ""
        token["sniper_gate_failures"] = ""
        return True, ""

    best_profile = "sniper_core" if len(core_failures) <= len(micro_failures) else "sniper_micro"
    best_failures = core_failures if best_profile == "sniper_core" else micro_failures
    rank_threshold = _PUMP_EARLY_SNIPER_MIN_RANK_SCORE if best_profile == "sniper_core" else _PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE
    token["entry_lane"] = "pump_early_reject"
    token["sniper_gate_profile"] = best_profile
    token["live_profit_gate_profile"] = best_profile
    token["live_rank_gate_threshold"] = rank_threshold
    token["live_rank_gate_source"] = "sniper_policy"
    token["live_rank_gate_margin"] = max(0.0, rank_threshold - rank_score)
    token["live_profit_gate_failed_count"] = int(len(best_failures))
    token["live_profit_gate_failures"] = ",".join(best_failures[:8])
    token["sniper_gate_failures"] = ",".join(best_failures[:8])
    return False, f"live_profit_gate:{best_profile}:{','.join(best_failures[:4])}"


def _aggressive_pump_gate(
    token: dict,
    rank_info: dict[str, object] | None,
    *,
    label: str,
    min_age_min: float,
    min_liquidity_usd: float,
    min_market_cap_usd: float,
    max_market_cap_usd: float,
    min_score_total: int,
    min_rank_score: float,
    min_txns_5m: int,
    max_snapshot_missing_fields: int,
    max_price_impact_pct: float,
    require_route: bool,
    require_price: bool,
) -> tuple[bool, str]:
    failures: list[str] = []
    has_route = bool(_metric_int(token, "has_jupiter_route"))
    if require_route and not has_route:
        failures.append("route_required")
    if require_price and _metric_float(token, "price_usd") <= 0:
        failures.append("price_missing")

    _add_min_failure(failures, "age", _candidate_age_minutes(token), min_age_min)
    _add_min_failure(failures, "liq", _metric_float(token, "liquidity_usd"), min_liquidity_usd)
    _add_min_failure(failures, "mcap", _metric_float(token, "market_cap_usd"), min_market_cap_usd)
    _add_max_failure(failures, "mcap", _metric_float(token, "market_cap_usd"), max_market_cap_usd)
    _add_min_failure(
        failures,
        "score",
        float(_metric_int(token, "score_total")),
        float(min_score_total),
    )
    _add_min_failure(
        failures,
        "rank",
        _sniper_rank_score(rank_info),
        min_rank_score,
    )
    _add_min_failure(
        failures,
        "txns5m",
        float(_metric_int(token, "txns_last_5m")),
        float(min_txns_5m),
    )
    _add_max_failure(
        failures,
        "impact",
        max(0.0, _metric_float(token, "price_impact_pct")),
        max_price_impact_pct,
    )
    snapshot_missing = max(0, _metric_int(token, "snapshot_missing_fields"))
    if snapshot_missing > max_snapshot_missing_fields:
        failures.append(f"missing>{max_snapshot_missing_fields}")
    failures.extend(_aggressive_research_guard_failures(token))

    token[f"{label}_gate_failed_count"] = int(len(failures))
    token[f"{label}_gate_failures"] = ",".join(failures[:8])
    if failures:
        token["entry_lane"] = "pump_early_sniper_research"
        token["gate_profile"] = f"{label}_research_guard"
        token["sniper_gate_profile"] = token.get("sniper_gate_profile") or label
        token["live_profit_gate_profile"] = f"{label}_research_guard"
        token["live_profit_gate_failed_count"] = int(len(failures))
        token["live_profit_gate_failures"] = ",".join(failures[:8])
        token["profit_gate_reject_reasons"] = ",".join(failures[:8])
        token["mcap_bucket"] = _mcap_bucket(_metric_float(token, "market_cap_usd"))
        token["price5m_bucket"] = _price5m_bucket(_metric_optional_float(token, "price_pct_5m"))
        return False, f"{label}_gate:{','.join(failures[:4])}"

    token["entry_lane"] = str(token.get("entry_lane") or "pump_early_sniper_research")
    if token["entry_lane"] in {"pump_early_reject", "unknown", ""}:
        token["entry_lane"] = "pump_early_sniper_research"
    token["gate_profile"] = f"{label}_research_buy"
    token["sniper_gate_profile"] = token.get("sniper_gate_profile") or label
    token["live_profit_gate_profile"] = f"{label}_research_buy"
    token["profit_lane_tier"] = token.get("profit_lane_tier") or label
    token["live_profit_gate_failed_count"] = 0
    token["live_profit_gate_failures"] = ""
    token["blocked_bucket"] = None
    token["mcap_bucket"] = _mcap_bucket(_metric_float(token, "market_cap_usd"))
    token["price5m_bucket"] = _price5m_bucket(_metric_optional_float(token, "price_pct_5m"))
    token["venue_is_pumpswap"] = int(_gate_dex_id(token) == "pumpswap")
    token["liquidity_is_proxy"] = int(_is_liquidity_proxy(token))
    token["impact_zero_flag"] = int(max(0.0, _metric_float(token, "price_impact_pct")) == 0.0)
    return True, ""


def _paper_aggressive_pump_gate(token: dict, rank_info: dict[str, object] | None = None) -> tuple[bool, str]:
    if not (DRY_RUN and _PAPER_AGGRESSIVE_TRADING_ENABLED and _PAPER_AGGRESSIVE_BUY_RESEARCH_LANES):
        return False, "paper_aggressive_disabled"
    return _aggressive_pump_gate(
        token,
        rank_info,
        label="paper_aggressive",
        min_age_min=_PAPER_AGGRESSIVE_MIN_AGE_MIN,
        min_liquidity_usd=_PAPER_AGGRESSIVE_MIN_LIQUIDITY_USD,
        min_market_cap_usd=_PAPER_AGGRESSIVE_MIN_MARKET_CAP_USD,
        max_market_cap_usd=_PAPER_AGGRESSIVE_MAX_MARKET_CAP_USD,
        min_score_total=_PAPER_AGGRESSIVE_MIN_SCORE_TOTAL,
        min_rank_score=_PAPER_AGGRESSIVE_MIN_RANK_SCORE,
        min_txns_5m=_PAPER_AGGRESSIVE_MIN_TXNS_5M,
        max_snapshot_missing_fields=_PAPER_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS,
        max_price_impact_pct=_PAPER_AGGRESSIVE_MAX_PRICE_IMPACT_PCT,
        require_route=_PAPER_AGGRESSIVE_REQUIRE_ROUTE,
        require_price=_PAPER_AGGRESSIVE_REQUIRE_PRICE,
    )


def _live_aggressive_pump_gate(token: dict, rank_info: dict[str, object] | None = None) -> tuple[bool, str]:
    if not ((not DRY_RUN) and _LIVE_AGGRESSIVE_TRADING_ENABLED and _LIVE_AGGRESSIVE_BUY_RESEARCH_LANES):
        return False, "live_aggressive_disabled"
    return _aggressive_pump_gate(
        token,
        rank_info,
        label="live_aggressive",
        min_age_min=_LIVE_AGGRESSIVE_MIN_AGE_MIN,
        min_liquidity_usd=_LIVE_AGGRESSIVE_MIN_LIQUIDITY_USD,
        min_market_cap_usd=_LIVE_AGGRESSIVE_MIN_MARKET_CAP_USD,
        max_market_cap_usd=_LIVE_AGGRESSIVE_MAX_MARKET_CAP_USD,
        min_score_total=_LIVE_AGGRESSIVE_MIN_SCORE_TOTAL,
        min_rank_score=_LIVE_AGGRESSIVE_MIN_RANK_SCORE,
        min_txns_5m=_LIVE_AGGRESSIVE_MIN_TXNS_5M,
        max_snapshot_missing_fields=_LIVE_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS,
        max_price_impact_pct=_LIVE_AGGRESSIVE_MAX_PRICE_IMPACT_PCT,
        require_route=_LIVE_AGGRESSIVE_REQUIRE_ROUTE,
        require_price=_LIVE_AGGRESSIVE_REQUIRE_PRICE,
    )


def _entry_quality_gate(
    token: dict,
    regime: str,
    quality_points: int = 0,
    rank_info: dict[str, object] | None = None,
    paper_cold_start_active: bool | None = None,
) -> tuple[bool, str]:
    if regime == "pump_early":
        if _PUMP_EARLY_SNIPER_ENABLED:
            ok, reason = _tag_pump_sniper_gate(token, rank_info)
            if ok:
                return True, ""
            aggressive_ok, aggressive_reason = _paper_aggressive_pump_gate(token, rank_info)
            if aggressive_ok:
                return True, ""
            live_aggressive_ok, live_aggressive_reason = _live_aggressive_pump_gate(token, rank_info)
            if live_aggressive_ok:
                return True, ""
            if aggressive_reason and "research_" in str(aggressive_reason):
                return False, aggressive_reason
            if live_aggressive_reason and "research_" in str(live_aggressive_reason):
                return False, live_aggressive_reason
            return False, reason or aggressive_reason or live_aggressive_reason

        hard_failures: list[str] = []
        route_required = bool(token.get("require_jupiter_for_buy", True))
        has_route = bool(_metric_int(token, "has_jupiter_route"))
        use_paper_cold_start = (
            _paper_cold_start_active()
            if paper_cold_start_active is None
            else bool(paper_cold_start_active)
        )
        min_age = _PUMP_EARLY_LIVE_MIN_AGE_EFFECTIVE
        min_liq = _PUMP_EARLY_LIVE_MIN_LIQUIDITY_EFFECTIVE
        min_score = _PUMP_EARLY_LIVE_MIN_SCORE_EFFECTIVE
        min_mcap = _PUMP_EARLY_LIVE_MIN_MARKET_CAP_EFFECTIVE
        max_missing = _PUMP_EARLY_LIVE_MAX_SNAPSHOT_MISSING_FIELDS
        if use_paper_cold_start:
            min_age = _PAPER_COLD_START_MIN_AGE_MIN
            min_liq = _PAPER_COLD_START_MIN_LIQUIDITY_USD
            min_score = _PAPER_COLD_START_MIN_SCORE_TOTAL
            min_mcap = _PAPER_COLD_START_MIN_MARKET_CAP_USD
            max_missing = _PAPER_COLD_START_MAX_SNAPSHOT_MISSING_FIELDS
        token["live_profit_gate_profile"] = "paper_cold_start" if use_paper_cold_start else "live_canary"

        def add_hard_min(name: str, value: float, threshold: float) -> None:
            if threshold > 0 and value < threshold:
                hard_failures.append(f"{name}<{threshold:g}")

        def add_hard_max(name: str, value: float, threshold: float) -> None:
            if threshold > 0 and value > threshold:
                hard_failures.append(f"{name}>{threshold:g}")

        if route_required and not has_route:
            hard_failures.append("route_required")

        add_hard_min("age", _candidate_age_minutes(token), min_age)
        add_hard_min("liq", _metric_float(token, "liquidity_usd"), min_liq)
        add_hard_min("score", float(_metric_int(token, "score_total")), min_score)
        add_hard_min("mcap", _metric_float(token, "market_cap_usd"), min_mcap)
        add_hard_max("mcap", _metric_float(token, "market_cap_usd"), _PUMP_EARLY_LIVE_HARD_MAX_MARKET_CAP_USD)
        add_hard_max(
            "impact",
            max(0.0, _metric_float(token, "price_impact_pct")),
            _PUMP_EARLY_LIVE_HARD_MAX_PRICE_IMPACT_PCT,
        )
        snapshot_missing = max(0, _metric_int(token, "snapshot_missing_fields"))
        if snapshot_missing > max_missing:
            hard_failures.append(f"missing>{max_missing}")
        if use_paper_cold_start:
            price_pct_5m = _metric_optional_float(token, "price_pct_5m")
            if price_pct_5m is None:
                if _PAPER_COLD_START_REQUIRE_PRICE_PCT_5M:
                    hard_failures.append("price5m_missing")
            else:
                if price_pct_5m < _PAPER_COLD_START_MIN_PRICE_PCT_5M:
                    hard_failures.append(f"price5m<{_PAPER_COLD_START_MIN_PRICE_PCT_5M:g}")
                if _PAPER_COLD_START_MAX_PRICE_PCT_5M > 0 and price_pct_5m > _PAPER_COLD_START_MAX_PRICE_PCT_5M:
                    hard_failures.append(f"price5m>{_PAPER_COLD_START_MAX_PRICE_PCT_5M:g}")

        rank_gate = research_runtime.load_live_rank_gate(regime)
        rank_threshold = (
            float(_PAPER_COLD_START_MIN_RANK_SCORE)
            if use_paper_cold_start
            else float(rank_gate.get("threshold") or 0.0)
        )
        rank_score = float((rank_info or {}).get("rank_score") or 0.0)
        rank_margin = max(0.0, rank_threshold - rank_score)
        token["live_rank_gate_threshold"] = rank_threshold
        token["live_rank_gate_source"] = (
            "paper_cold_start"
            if use_paper_cold_start
            else str(rank_gate.get("source") or "fallback")
        )
        token["live_rank_gate_margin"] = float(rank_margin)
        if rank_threshold > 0 and rank_score < rank_threshold:
            hard_failures.append(f"rank<{rank_threshold:.1f}")

        token["live_profit_gate_failed_count"] = int(len(hard_failures))
        token["live_profit_gate_failures"] = ",".join(hard_failures[:8])
        if hard_failures:
            return False, f"live_profit_gate:{','.join(hard_failures[:4])}"

        min_points = _PUMP_EARLY_QUALITY_MIN_POINTS
        if min_points <= 0:
            return True, ""

        checks: list[tuple[str, bool, float, float]] = []

        def add_check(name: str, value: float, threshold: float) -> None:
            if threshold <= 0:
                return
            checks.append((name, value >= threshold, value, threshold))

        def add_max_check(name: str, value: float, ceiling: float) -> None:
            if ceiling <= 0:
                return
            checks.append((name, value <= ceiling, value, ceiling))

        add_check("age", _candidate_age_minutes(token), _PUMP_EARLY_QUALITY_MIN_AGE_MIN)
        add_check("liq", _metric_float(token, "liquidity_usd"), _PUMP_EARLY_QUALITY_MIN_LIQUIDITY_USD)
        add_check("vol", _metric_float(token, "volume_24h_usd"), _PUMP_EARLY_QUALITY_MIN_VOLUME_USD_24H)
        add_check("mcap", _metric_float(token, "market_cap_usd"), _PUMP_EARLY_QUALITY_MIN_MARKET_CAP_USD)
        add_check("holders", float(_metric_int(token, "holders")), float(_PUMP_EARLY_QUALITY_MIN_HOLDERS))
        add_check("score", float(_metric_int(token, "score_total")), float(_PUMP_EARLY_QUALITY_MIN_SCORE_TOTAL))
        add_check("qpts", float(max(0, int(quality_points))), float(min_points))
        add_max_check(
            "impact",
            max(0.0, _metric_float(token, "price_impact_pct")),
            _PUMP_EARLY_QUALITY_MAX_PRICE_IMPACT_PCT,
        )

        if not checks:
            return True, ""

        passed = sum(1 for _name, ok, _value, _thr in checks if ok)
        required = min(min_points, len(checks))
        if passed >= required:
            return True, ""

        failed = [f"{name}{'<=' if name == 'impact' else '<'}{thr:g}" for name, ok, _value, thr in checks if not ok]
        return False, f"pump_quality={passed}/{required} failed={','.join(failed[:4])}"

    if regime != "dex_mature" or _DEX_MATURE_QUALITY_MIN_POINTS <= 0:
        return True, ""

    checks: list[tuple[str, bool, float, float]] = []

    def add_check(name: str, value: float, threshold: float) -> None:
        if threshold <= 0:
            return
        checks.append((name, value >= threshold, value, threshold))

    add_check("age", _candidate_age_minutes(token), _DEX_MATURE_QUALITY_MIN_AGE_MIN)
    add_check("liq", _metric_float(token, "liquidity_usd"), _DEX_MATURE_QUALITY_MIN_LIQUIDITY_USD)
    add_check("vol", _metric_float(token, "volume_24h_usd"), _DEX_MATURE_QUALITY_MIN_VOLUME_USD_24H)
    add_check("mcap", _metric_float(token, "market_cap_usd"), _DEX_MATURE_QUALITY_MIN_MARKET_CAP_USD)
    add_check("holders", float(_metric_int(token, "holders")), float(_DEX_MATURE_QUALITY_MIN_HOLDERS))
    add_check("score", float(_metric_int(token, "score_total")), float(_DEX_MATURE_QUALITY_MIN_SCORE_TOTAL))

    if not checks:
        return True, ""

    min_points = min(_DEX_MATURE_QUALITY_MIN_POINTS, len(checks))
    passed = sum(1 for _name, ok, _value, _thr in checks if ok)
    if passed >= min_points:
        return True, ""

    failed = [f"{name}<{thr:g}" for name, ok, _value, thr in checks if not ok]
    return False, f"quality_points={passed}/{min_points} failed={','.join(failed[:4])}"


async def _closed_position_count(ses: SessionLocal) -> int:
    try:
        closed_count = await ses.scalar(
            select(func.count()).select_from(Position).where(Position.closed.is_(True))
        )
        return int(closed_count or 0)
    except Exception as exc:
        log.debug("paper cold-start closed-count fallback: %s", exc)
        return int(_stats.get("sold", 0) or 0)


def _store_policy_reject(
    sample: dict,
    *,
    already_vector: bool = False,
    reason: str = "",
) -> None:
    """
    Persiste rechazos de política/heurística sin mezclarlos con outcomes reales.
    """
    addr = _sample_address(sample)
    dedup_key = f"{addr}:{reason or 'policy_reject'}" if addr else ""
    if dedup_key and _POLICY_REJECT_DEDUP_TTL_S > 0:
        now = time.monotonic()
        last = _policy_reject_seen.get(dedup_key, 0.0)
        if (now - last) < _POLICY_REJECT_DEDUP_TTL_S:
            return
        _policy_reject_seen[dedup_key] = now
    vec = sample if already_vector else build_feature_vector(sample)
    store_append(vec, 0, sample_type="policy_reject")
    _stats["filtered_immediate_0"] += 1


def _research_stage(
    token: dict,
    *,
    stage: str,
    proba: float | None = None,
    threshold: float | None = None,
    rank_info: dict[str, object] | None = None,
) -> None:
    try:
        research_runtime.record_candidate_stage(
            token,
            stage=stage,
            proba=proba,
            threshold=threshold,
            rank_info=rank_info,
        )
    except Exception as exc:
        log.debug("research stage %s %s → %s", stage, str(token.get("address") or "")[:6], exc)


def _research_decision(
    token: dict,
    *,
    action: str,
    reason: str,
    stage: str,
    proba: float | None = None,
    threshold: float | None = None,
    rank_info: dict[str, object] | None = None,
    shadow_kind: str | None = None,
    dedup_ttl_s: int | None = None,
) -> None:
    try:
        research_runtime.record_candidate_decision(
            token,
            action=action,
            reason=reason,
            stage=stage,
            proba=proba,
            threshold=threshold,
            rank_info=rank_info,
            shadow_kind=shadow_kind,
            dedup_ttl_s=dedup_ttl_s,
        )
    except Exception as exc:
        log.debug("research decision %s %s %s → %s", action, reason, str(token.get("address") or "")[:6], exc)


async def _maybe_open_research_shadow(
    token: dict,
    vec: dict,
    *,
    reason: str,
    proba: float | None = None,
    threshold: float | None = None,
    rank_info: dict[str, object] | None = None,
    soft_score_min: int = 0,
    stage: str = "decision",
) -> bool:
    try:
        should_open, _shadow_reason = research_runtime.should_open_shadow(
            token,
            action="rejected",
            reason=reason,
            proba=proba,
            threshold=threshold,
            rank_info=rank_info,
            soft_score_min=soft_score_min,
        )
    except Exception as exc:
        log.debug("research should_open_shadow %s → %s", str(token.get("address") or "")[:6], exc)
        return False

    if not should_open:
        return False

    shadow_vec = vec
    try:
        shadow_vec = vec.to_dict() if hasattr(vec, "to_dict") else dict(vec)
        for key in (
            "entry_lane",
            "sniper_gate_profile",
            "sniper_gate_failures",
            "gate_profile",
            "live_profit_gate_profile",
            "live_profit_gate_failures",
            "live_profit_gate_failed_count",
            "profit_gate_reject_reasons",
            "blocked_bucket",
            "liquidity_is_proxy",
            "liquidity_usd_is_proxy",
            "mcap_bucket",
            "price5m_bucket",
            "venue_is_pumpswap",
            "impact_zero_flag",
        ):
            if token.get(key) is not None:
                shadow_vec[key] = token.get(key)
    except Exception:
        shadow_vec = vec

    await _open_shadow(
        str(token.get("address") or ""),
        shadow_vec,
        price_hint=token.get("price_usd"),
        force=True,
        regime=str(token.get("entry_regime") or "dex_mature"),
        reason=reason,
        stage=stage,
        proba=proba,
        threshold=threshold,
        rank_info=rank_info,
        shadow_kind="research",
    )
    return True

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
    global _wallet_sol_balance, _last_wallet_check, _last_wallet_checked_at

    if now_mono - _last_wallet_check < WALLET_POLL_INTERVAL:
        return
    try:
        _wallet_sol_balance = await get_sol_balance()
        _last_wallet_check  = now_mono
        _last_wallet_checked_at = utc_now()
        log.debug("💰 Wallet = %.3f SOL", _wallet_sol_balance)
    except Exception as exc:  # noqa: BLE001
        _note_runtime_error("refresh_balance", exc)
        log.warning("get_sol_balance → %s", exc)

async def _refresh_balance_force(tag: str = "") -> None:
    """Refresco inmediato (post-trade). Evita el bug del 'fake update'."""
    global _wallet_sol_balance, _last_wallet_check, _last_wallet_checked_at
    try:
        _wallet_sol_balance = await get_sol_balance()
        _last_wallet_check = time.monotonic()
        _last_wallet_checked_at = utc_now()
        if tag:
            log.debug("💰 Wallet refrescada (%s) = %.3f SOL", tag, _wallet_sol_balance)
    except Exception as exc:
        _note_runtime_error(f"refresh_balance_force[{tag or 'untagged'}]", exc)
        log.debug("refresh_balance_force(%s) → %s", tag, exc)

def _compute_trade_amount(size_multiplier: float = 1.0) -> float:
    """
    Cuántos SOL destinar a la próxima compra.

    • En DRY_RUN se ignora el balance: siempre usa TRADE_AMOUNT_SOL.
    • En modo real se respeta la reserva de gas y se hace un
      sanity-check para no bajar de MIN_SOL_BALANCE ni de MIN_BUY_SOL.
    """
    multiplier = max(0.0, float(size_multiplier or 0.0))
    # TRADE_AMOUNT_SOL is the effective per-trade spend. Multipliers are kept
    # for bucket/health metadata, but they must not silently shrink live/paper buys.
    desired_amount = max(0.0, float(TRADE_AMOUNT_SOL_CFG)) if multiplier > 0.0 else 0.0
    if desired_amount > 0.0:
        desired_amount = max(float(MIN_BUY_SOL), desired_amount)

    # — Paper-trading —
    if DRY_RUN:
        return desired_amount

    # — Real-trading —
    usable = max(0.0, _wallet_sol_balance - GAS_RESERVE_SOL)

    # si al restar la compra quedaríamos por debajo de los umbrales, abortamos
    if usable < max(MIN_BUY_SOL, MIN_SOL_BALANCE):
        return 0.0

    # gastamos el menor de (importe deseado, saldo disponible)
    return min(desired_amount, usable)


# ╭─────────────────────── Labeler periódico ────────────────────────────────╮
async def _periodic_labeler() -> None:
    while True:
        try:
            await label_positions()
        except Exception as exc:
            log.error("label_positions → %s", exc)
        await asyncio.sleep(3600)


def _path_mtime_utc(path_like) -> Optional[dt.datetime]:
    path = getattr(path_like, "resolve", lambda: path_like)()
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    except Exception:
        return None


def _read_json_file(path_like) -> Optional[dict]:
    try:
        path = getattr(path_like, "resolve", lambda: path_like)()
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _coerce_utc_datetime(value) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        parsed = parse_iso_utc(value)
        if parsed is not None:
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)
        try:
            fallback = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return fallback if fallback.tzinfo is not None else fallback.replace(tzinfo=dt.timezone.utc)
        except Exception:
            return None
    return None


def _shadow_counts_by_regime() -> dict[str, int]:
    counts = {"pump_early": 0, "dex_mature": 0, "revival": 0}
    for payload in _shadow_positions.values():
        regime = str(payload.get("regime") or "dex_mature")
        if regime not in counts:
            counts[regime] = 0
        counts[regime] += 1
    return counts


def _queue_snapshot_payload(now: dt.datetime) -> tuple[Optional[dt.datetime], dict[str, object]]:
    raw_items = lista_pares.snapshot(limit=CFG.MAX_QUEUE_SIZE)
    oldest_first_seen_at = _coerce_utc_datetime(lista_pares.oldest_first_seen())
    items: list[dict[str, object]] = []
    for raw in raw_items:
        first_seen_at = _coerce_utc_datetime(raw.get("first_seen"))
        next_retry_at = _coerce_utc_datetime(raw.get("next_try"))
        discovered_at = _coerce_utc_datetime(raw.get("discovered_at"))
        items.append(
            {
                "address": str(raw.get("address") or ""),
                "symbol": raw.get("symbol"),
                "status": str(raw.get("status") or "pending"),
                "discovered_via": raw.get("discovered_via"),
                "entry_regime": raw.get("entry_regime"),
                "dex_id": raw.get("dex_id"),
                "first_seen_at": first_seen_at,
                "discovered_at": discovered_at,
                "attempts": int(raw.get("attempts", 0) or 0),
                "retries_left": int(raw.get("retries_left", 0) or 0),
                "next_retry_at": next_retry_at if str(raw.get("status") or "") == "cooldown" else None,
                "last_reason": str(raw.get("reason", "") or ""),
                "queue_age_minutes": float(raw.get("queue_age_minutes", 0.0) or 0.0),
            }
        )

    return oldest_first_seen_at, {"captured_at": now, "items": items}


def _research_runtime_snapshot() -> dict[str, object]:
    scorecard = _read_json_file(research_runtime.RESEARCH_SCORECARD_JSON)
    thresholds = _read_json_file(research_runtime.RESEARCH_THRESHOLDS_JSON)
    scorecard_generated_at = _coerce_utc_datetime(scorecard.get("generated_at_utc")) if scorecard else None
    thresholds_generated_at = _coerce_utc_datetime(thresholds.get("generated_at_utc")) if thresholds else None
    return {
        "lane_enabled": bool(getattr(CFG, "RESEARCH_LANE_ENABLED", True)),
        "shadow_enabled": bool(getattr(CFG, "RESEARCH_SHADOW_ENABLED", True)),
        "open_shadow_count": len(_shadow_positions),
        "open_shadow_by_regime": _shadow_counts_by_regime(),
        "scorecard_generated_at": scorecard_generated_at,
        "thresholds_generated_at": thresholds_generated_at,
        "last_event_at": _path_mtime_utc(research_runtime.RESEARCH_EVENTS_PATH),
    }


def _build_info_payload() -> dict[str, object]:
    return {
        "app": "memebot3",
        "bot_version": "local",
        "python_version": sys.version.split()[0],
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "git_sha": os.getenv("GIT_SHA"),
    }


async def _build_runtime_state_snapshot() -> RuntimeStateSnapshot:
    now = utc_now()
    queue_pending, queue_requeued, queue_cooldown = queue_stats()
    queue_oldest_first_seen_at, queue_items = _queue_snapshot_payload(now)
    open_positions_count = await count_open_positions(SessionLocal)
    model_status = model_runtime_status()
    ml_gate = _ml_gate_state()
    strategy_health = strategy_runtime.describe_regime_health(now)
    bucket_health = strategy_runtime.describe_bucket_health(now)
    blocked_buckets = {
        key: value
        for key, value in bucket_health.items()
        if bool((value or {}).get("blocked"))
    }
    research_snapshot = _research_runtime_snapshot()
    productive_health = dict(strategy_health.get("pump_early") or {})
    strategy_health_payload = {
        **strategy_health,
        "productive_health": productive_health,
        "research_health": {
            "lane_enabled": bool(research_snapshot.get("lane_enabled", True)),
            "shadow_enabled": bool(research_snapshot.get("shadow_enabled", True)),
            "open_shadow_count": research_snapshot.get("open_shadow_count"),
            "open_shadow_by_regime": research_snapshot.get("open_shadow_by_regime") or {},
            "last_event_at": research_snapshot.get("last_event_at"),
        },
        "current_gate_rebased": bool(productive_health.get("current_gate_rebased")),
        "recovery_basis": productive_health.get("recovery_basis") or {},
        "bucket_health": bucket_health,
        "blocked_buckets": blocked_buckets,
    }
    pump_health = productive_health
    research_rank_gate = research_runtime.load_live_rank_gate("pump_early", now=now)
    live_rank_gate = {
        "source": "pumpswap_profit_policy" if _PUMP_EARLY_PROFIT_LANE_ENABLED else (
            "sniper_policy" if _PUMP_EARLY_SNIPER_ENABLED else str(research_rank_gate.get("source") or "fallback")
        ),
        "enabled": True,
        "sniper_enabled": bool(_PUMP_EARLY_SNIPER_ENABLED),
        "profit_lane_enabled": bool(_PUMP_EARLY_PROFIT_LANE_ENABLED),
        "core_threshold": float(_PUMP_EARLY_SNIPER_MIN_RANK_SCORE),
        "micro_threshold": float(_PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE),
        "research_rank_gate": research_rank_gate,
    }
    stats_payload = {
        **_stats,
        "last_buy_at": _last_buy_at,
        "last_sell_at": _last_sell_at,
        "pending_ai_vectors": len(_pending_ai_vectors),
        "open_shadow_positions": len(_shadow_positions),
    }
    ml_gate_payload = {
        "mode": ml_gate.get("mode"),
        "enforced": bool(ml_gate.get("enforce")),
        "threshold": float(AI_THRESHOLD),
        "activation_ready": ml_gate.get("activation_ready"),
        "dataset_quality_passed": model_status.get("dataset_quality_passed"),
        "model_loaded": bool(model_status.get("model_loaded")),
        "model_exists": bool(model_status.get("model_exists")),
        "meta_exists": bool(model_status.get("meta_exists")),
        "features_count": int(model_status.get("features_count") or 0),
        "threshold_metric": model_status.get("threshold_metric"),
        "rows": model_status.get("rows"),
        "eligible_rows": model_status.get("eligible_rows"),
        "eligible_unique_tokens": model_status.get("eligible_unique_tokens"),
        "eligible_positives": model_status.get("eligible_positives"),
        "holdout_rows": model_status.get("holdout_rows"),
        "rows_missing_lane_metadata": model_status.get("rows_missing_lane_metadata"),
        "last_train_attempt_at": model_status.get("last_train_attempt_at"),
        "last_train_status": model_status.get("last_train_status"),
        "skip_reasons": model_status.get("skip_reasons"),
        "rows_to_next_model": model_status.get("rows_to_next_model"),
        "blocker": model_status.get("blocker"),
        "live_rank_gate": live_rank_gate,
        "live_threshold_origin": str(live_rank_gate.get("source") or "fallback"),
        "live_uses_rank_score": False if _PUMP_EARLY_PROFIT_LANE_ENABLED else True,
        "live_uses_heuristic_only": True if _PUMP_EARLY_PROFIT_LANE_ENABLED else False,
        "last_auto_demote_at": pump_health.get("last_auto_demote_at"),
        "last_auto_recover_at": pump_health.get("last_auto_recover_at"),
        "last_reload_at": None,
        "last_decision_at": None,
    }

    wallet_sol = _wallet_sol_balance if _last_wallet_checked_at is not None else None
    return RuntimeStateSnapshot(
        bot_id=_RUNTIME_STATE_BOT_ID,
        updated_at=now,
        heartbeat_at=now,
        started_at=_runtime_started_at,
        process_state=_effective_runtime_process_state(now),
        dry_run=bool(DRY_RUN),
        discovery_paused=bool(_runtime_discovery_paused),
        buys_paused=bool(_runtime_buys_paused),
        retrain_state=_runtime_retrain_state,
        reports_refresh_state=_runtime_reports_refresh_state,
        wallet_sol=wallet_sol,
        wallet_checked_at=_last_wallet_checked_at,
        open_positions_count=int(open_positions_count),
        queue_pending=int(queue_pending),
        queue_requeued=int(queue_requeued),
        queue_cooldown=int(queue_cooldown),
        queue_oldest_first_seen_at=queue_oldest_first_seen_at,
        buy_limiter_in_window=int(_BUY_LIMITER.current()),
        buy_limiter_window_s=int(_BUY_LIMITER.window_s),
        discovery_last_ok_at=_last_discovery_ok_at,
        monitor_last_ok_at=_last_monitor_ok_at,
        last_error=_runtime_last_error,
        last_error_at=_runtime_last_error_at,
        stats=stats_payload,
        ml_gate=ml_gate_payload,
        strategy_health=strategy_health_payload,
        research=research_snapshot,
        queue_items=queue_items,
        build_info=_build_info_payload(),
    )


async def _publish_runtime_state_once() -> None:
    snapshot = await _build_runtime_state_snapshot()
    await publish_runtime_state(SessionLocal, snapshot)


async def runtime_state_loop() -> None:
    while True:
        try:
            await _publish_runtime_state_once()
        except Exception as exc:
            _note_runtime_error("runtime_state_publish", exc)
            log.warning("runtime state publish → %s", exc)
        await asyncio.sleep(_RUNTIME_STATE_INTERVAL_S)


def _current_logger_level_name(target_logger: logging.Logger) -> str:
    level = int(target_logger.level or target_logger.getEffectiveLevel())
    name = logging.getLevelName(level)
    return name if isinstance(name, str) else str(level)


async def _reload_model_now() -> dict[str, object]:
    status_before = model_runtime_status()
    if not status_before.get("model_exists") and not status_before.get("meta_exists"):
        raise RuntimeError("model_artifacts_missing")

    await asyncio.to_thread(reload_model)
    threshold_info = _apply_ai_threshold_override()
    status_after = model_runtime_status()
    return {
        "model_exists": bool(status_after.get("model_exists")),
        "meta_exists": bool(status_after.get("meta_exists")),
        "model_loaded": bool(status_after.get("model_loaded")),
        "features_count": int(status_after.get("features_count") or 0),
        "threshold_metric": status_after.get("threshold_metric"),
        "rows": status_after.get("rows"),
        "threshold": float(AI_THRESHOLD),
        "threshold_override": threshold_info,
    }


async def _run_retrain_once(*, source: str, force_requested: bool = False) -> dict[str, object]:
    global _runtime_retrain_state

    if _retrain_lock.locked():
        raise RuntimeError("retrain_already_running")

    success = False
    async with _retrain_lock:
        _runtime_retrain_state = "running"
        try:
            trained = bool(await asyncio.to_thread(retrain_if_better))
            reload_result = await _reload_model_now() if trained else None
            scorecard_result = (
                await asyncio.to_thread(lambda: research_runtime.refresh_scorecard(force=True))
                if trained
                else None
            )
            if trained:
                log.info("Retrain completo; modelo recargado en memoria y scorecard refrescado")
            success = True
            return {
                "trained": trained,
                "force_requested": bool(force_requested),
                "reload": reload_result,
                "scorecard_refreshed": bool(scorecard_result),
            }
        except Exception as exc:
            _runtime_retrain_state = "failed"
            _note_runtime_error(source, exc)
            raise
        finally:
            if success:
                _runtime_retrain_state = "idle"


async def _refresh_reports_once(
    *,
    source: str,
    force: bool,
    include: Sequence[str] | None = None,
) -> dict[str, object]:
    global _runtime_reports_refresh_state

    if _reports_refresh_lock.locked():
        raise RuntimeError("reports_refresh_already_running")

    selected = [str(item).strip().lower() for item in (include or ("baseline", "edge", "research")) if str(item).strip()]
    success = False
    async with _reports_refresh_lock:
        _runtime_reports_refresh_state = "running"
        try:
            reports: dict[str, object] = {}
            if "baseline" in selected:
                baseline_snapshot = await asyncio.to_thread(build_baseline_snapshot)
                baseline_markdown = await asyncio.to_thread(render_baseline_markdown, baseline_snapshot)
                baseline_path = PROJECT_ROOT / "docs" / "BASELINE.md"
                baseline_path.write_text(baseline_markdown, encoding="utf-8")
                reports["baseline"] = {
                    "path": str(baseline_path),
                    "positions_rows": int(((baseline_snapshot.get("positions") or {}).get("rows") or 0)),
                    "features_rows": int(((baseline_snapshot.get("features") or {}).get("rows") or 0)),
                }

            if "edge" in selected:
                edge_snapshot = await asyncio.to_thread(summarize_edge)
                edge_markdown = await asyncio.to_thread(render_edge_markdown, edge_snapshot)
                edge_path = PROJECT_ROOT / "docs" / "EDGE_REPORT.md"
                edge_path.write_text(edge_markdown, encoding="utf-8")
                reports["edge"] = {
                    "path": str(edge_path),
                    "closed_trades": int(((edge_snapshot.get("overview") or {}).get("closed_trades") or 0)),
                }

            if "research" in selected:
                research_snapshot = await asyncio.to_thread(
                    lambda: research_runtime.refresh_scorecard(force=bool(force))
                )
                reports["research"] = {
                    "path": str(research_runtime.RESEARCH_SCORECARD_JSON),
                    "force": bool(force),
                    "refreshed": research_snapshot is not None,
                }

            success = True
            return {
                "force": bool(force),
                "include": selected,
                "reports": reports,
            }
        except Exception as exc:
            _runtime_reports_refresh_state = "failed"
            _note_runtime_error(source, exc)
            raise
        finally:
            if success:
                _runtime_reports_refresh_state = "idle"


async def _execute_control_command(command: dict[str, object]) -> tuple[str, dict[str, object] | None, str | None]:
    global _runtime_buys_paused, _runtime_discovery_paused

    command_type = str(command.get("command_type") or "").strip().lower()
    payload = dict(command.get("payload") or {})

    if command_type == "pause_discovery":
        if _runtime_discovery_paused:
            return COMMAND_STATUS_REJECTED, {"discovery_paused": True, "reason": "already_paused"}, "already_paused"
        _runtime_discovery_paused = True
        log.info("Control command: discovery paused")
        return COMMAND_STATUS_DONE, {"discovery_paused": True}, None

    if command_type == "resume_discovery":
        if not _runtime_discovery_paused:
            return COMMAND_STATUS_REJECTED, {"discovery_paused": False, "reason": "already_live"}, "already_live"
        _runtime_discovery_paused = False
        log.info("Control command: discovery resumed")
        return COMMAND_STATUS_DONE, {"discovery_paused": False}, None

    if command_type == "pause_buys":
        if _runtime_buys_paused:
            return COMMAND_STATUS_REJECTED, {"buys_paused": True, "reason": "already_paused"}, "already_paused"
        _runtime_buys_paused = True
        log.info("Control command: buys paused")
        return COMMAND_STATUS_DONE, {"buys_paused": True}, None

    if command_type == "resume_buys":
        if not _runtime_buys_paused:
            return COMMAND_STATUS_REJECTED, {"buys_paused": False, "reason": "already_live"}, "already_live"
        _runtime_buys_paused = False
        log.info("Control command: buys resumed")
        return COMMAND_STATUS_DONE, {"buys_paused": False}, None

    if command_type == "reload_model":
        try:
            result = await _reload_model_now()
        except RuntimeError as exc:
            if str(exc) == "model_artifacts_missing":
                return COMMAND_STATUS_REJECTED, {"reason": "model_artifacts_missing"}, "model_artifacts_missing"
            raise
        log.info("Control command: model reloaded")
        return COMMAND_STATUS_DONE, result, None

    if command_type == "trigger_retrain":
        if _retrain_lock.locked() or _runtime_retrain_state == "running":
            return COMMAND_STATUS_REJECTED, {"reason": "retrain_already_running"}, "retrain_already_running"
        result = await _run_retrain_once(
            source="control_command.trigger_retrain",
            force_requested=bool(payload.get("force")),
        )
        return COMMAND_STATUS_DONE, result, None

    if command_type == "refresh_reports":
        if _reports_refresh_lock.locked() or _runtime_reports_refresh_state == "running":
            return COMMAND_STATUS_REJECTED, {"reason": "reports_refresh_already_running"}, "reports_refresh_already_running"
        result = await _refresh_reports_once(
            source="control_command.refresh_reports",
            force=bool(payload.get("force")),
            include=payload.get("include"),
        )
        return COMMAND_STATUS_DONE, result, None

    if command_type == "set_log_level":
        logger_name = str(payload.get("logger") or "root").strip() or "root"
        level_name = str(payload.get("level") or "INFO").strip().upper()
        target_logger = logging.getLogger() if logger_name == "root" else logging.getLogger(logger_name)
        before = _current_logger_level_name(target_logger)
        if before == level_name:
            return COMMAND_STATUS_REJECTED, {"logger": logger_name, "level": level_name, "reason": "already_set"}, "already_set"
        target_logger.setLevel(getattr(logging, level_name, logging.INFO))
        after = _current_logger_level_name(target_logger)
        log.info("Control command: logger=%s level=%s", logger_name, after)
        return COMMAND_STATUS_DONE, {"logger": logger_name, "before": before, "after": after}, None

    return COMMAND_STATUS_FAILED, None, f"unsupported command_type: {command_type}"


async def _process_next_control_command(*, bot_id: str = _RUNTIME_STATE_BOT_ID) -> bool:
    command = await claim_next_pending_command(SessionLocal, bot_id=bot_id)
    if command is None:
        return False

    command_id = int(command.get("id") or 0)
    command_type = str(command.get("command_type") or "unknown")
    final_status = COMMAND_STATUS_DONE
    result_payload: dict[str, object] | None = None
    error_text: str | None = None

    try:
        final_status, result_payload, error_text = await _execute_control_command(command)
    except Exception as exc:
        final_status = COMMAND_STATUS_FAILED
        result_payload = {"exception_type": exc.__class__.__name__}
        error_text = str(exc)
        _note_runtime_error(f"control_command.{command_type}", exc)
        log.error("control command %s failed: %s", command_type, exc, exc_info=True)

    await complete_command(
        SessionLocal,
        command_id,
        status=final_status,
        result=result_payload,
        error_text=error_text,
    )
    await _publish_runtime_state_once()
    log.info("Control command complete id=%s type=%s status=%s", command_id, command_type, final_status)
    return True


async def control_command_loop() -> None:
    while True:
        try:
            handled = await _process_next_control_command()
        except Exception as exc:
            handled = False
            _note_runtime_error("control_command_loop", exc)
            log.error("control_command_loop â†’ %s", exc)
        await asyncio.sleep(0.1 if handled else _CONTROL_COMMAND_POLL_INTERVAL_S)


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
async def _open_shadow(
    addr: str,
    vec: dict,
    price_hint: Optional[float] = None,
    *,
    force: bool = False,
    regime: str | None = None,
    reason: str = "",
    stage: str = "decision",
    proba: float | None = None,
    threshold: float | None = None,
    rank_info: dict[str, object] | None = None,
    shadow_kind: str = "execution",
) -> None:
    """Crea una shadow position (solo modo real) cuando pasa IA pero no se compra."""
    if not force and (DRY_RUN or not REAL_SHADOW_SIM):
        return
    try:
        if addr in _shadow_positions:
            return
        _pending_ai_vectors.pop(addr, None)
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
        opened_at = utc_now()
        token_ctx = dict(vec)
        token_ctx.setdefault("address", addr)
        token_ctx.setdefault("entry_regime", regime or "dex_mature")
        token_ctx.setdefault("score_total", vec.get("score_total"))
        runner_exit_profile = _runner_profile_for_subject(token_ctx)
        shadow_payload = {
            "vec": vec,
            "opened_at": opened_at,
            "buy_price_usd": float(price) if price is not None else None,
            "entry_regime": regime or "dex_mature",
            "highest_pnl_pct": 0.0,
            "max_pnl_pct_seen": 0.0,
            "peak_price_usd": float(price) if price is not None else None,
            "partial_taken": False,
            "remaining_fraction": 1.0,
            "realized_pnl_ratio": 0.0,
            "partial_fraction": 0.0,
            "time_to_partial_sec": None,
            "time_to_peak_sec": None,
            "peak_after_partial_pct": None,
            "exit_from_peak_giveback_pct": None,
            "runner_exit_profile": runner_exit_profile,
            "price_source": token_ctx.get("price_source"),
            "buy_liquidity_usd": token_ctx.get("liquidity_usd"),
            "reason": reason,
            "stage": stage,
            "ml_proba": proba,
            "threshold": threshold,
            "rank_info": rank_info or {},
            "shadow_kind": shadow_kind,
        }
        _shadow_positions[addr] = shadow_payload
        open_rank_info = rank_info or research_runtime.score_candidate(token_ctx, proba=proba, threshold=threshold)
        research_runtime.record_shadow_open(
            addr,
            payload={
                **open_rank_info,
                "regime": regime or "dex_mature",
                "opened_at": opened_at,
                "buy_price_usd": float(price) if price is not None else None,
                "reason": reason,
                "stage": stage,
                "ml_proba": proba,
                "threshold": threshold,
                "score_total": token_ctx.get("score_total"),
                "age_minutes": token_ctx.get("age_minutes") or token_ctx.get("age_min"),
                "liquidity_usd": token_ctx.get("liquidity_usd"),
                "volume_24h_usd": token_ctx.get("volume_24h_usd"),
                "market_cap_usd": token_ctx.get("market_cap_usd"),
                "holders": token_ctx.get("holders"),
                "discovered_via": token_ctx.get("discovered_via"),
                "entry_lane": token_ctx.get("entry_lane"),
                "sniper_gate_profile": token_ctx.get("sniper_gate_profile"),
                "sniper_gate_failures": token_ctx.get("sniper_gate_failures") or token_ctx.get("live_profit_gate_failures"),
                "gate_profile": token_ctx.get("gate_profile"),
                "liquidity_is_proxy": token_ctx.get("liquidity_is_proxy") or token_ctx.get("liquidity_usd_is_proxy"),
                "mcap_bucket": token_ctx.get("mcap_bucket"),
                "price5m_bucket": token_ctx.get("price5m_bucket"),
                "venue_is_pumpswap": token_ctx.get("venue_is_pumpswap"),
                "impact_zero_flag": token_ctx.get("impact_zero_flag"),
                "profit_gate_reject_reasons": token_ctx.get("profit_gate_reject_reasons"),
                "runner_exit_profile": runner_exit_profile,
            },
            shadow_kind=shadow_kind,
        )
        log.info("👻 Shadow %s creada: %s (buy_price_usd=%s)", shadow_kind, addr[:6], _fmt(price))
    except Exception as exc:
        log.debug("open_shadow %s → %s", addr[:6], exc)

async def _tick_shadows() -> None:
    """Revisa sombras paralelas y las cierra con una simulación ligera de la policy de exits."""
    if not _shadow_positions:
        return
    now = utc_now()
    to_delete: List[str] = []

    for addr, sd in list(_shadow_positions.items()):
        opened = sd.get("opened_at")
        if not opened:
            to_delete.append(addr)
            continue
        regime = str(sd.get("entry_regime") or "dex_mature")
        shadow_policy = exit_policy.effective_exit_policy(sd)
        buy_price = _to_float(sd.get("buy_price_usd"))

        tok = None
        try:
            tok = await price_service.get_price(addr, use_gt=_RESEARCH_SHADOW_USE_GECKO, allow_partial=True)
        except Exception:
            tok = None

        close_price = None
        liq_now = None
        if tok:
            close_price = _to_float(tok.get("price_usd"))
            liq_now = _to_float(tok.get("liquidity_usd"))
        if close_price is None:
            try:
                close_price = await price_service.get_price_usd(addr, use_gt=_RESEARCH_SHADOW_USE_GECKO)
            except Exception:
                close_price = None

        pnl_pct_total = None
        label = 0
        peak = float(sd.get("highest_pnl_pct") or 0.0)
        pnl_pct_live = None
        if buy_price and close_price:
            pnl_ratio_live = (float(close_price) - float(buy_price)) / float(buy_price)
            pnl_pct_live = float(pnl_ratio_live * 100.0)
            if pnl_pct_live > peak:
                peak = pnl_pct_live
                sd["highest_pnl_pct"] = peak
                sd["max_pnl_pct_seen"] = peak
                sd["peak_price_usd"] = float(close_price)
                peak_age_s = _seconds_from_opened_at(opened, now)
                if peak_age_s is not None:
                    sd["time_to_peak_sec"] = peak_age_s
                if bool(sd.get("partial_taken")):
                    current_peak_after_partial = _to_float(sd.get("peak_after_partial_pct"), 0.0) or 0.0
                    if peak > current_peak_after_partial:
                        sd["peak_after_partial_pct"] = peak

        subject = SimpleNamespace(
            entry_regime=regime,
            opened_at=opened,
            buy_price_usd=buy_price,
            buy_liquidity_usd=sd.get("buy_liquidity_usd"),
            highest_pnl_pct=peak,
            max_pnl_pct_seen=sd.get("max_pnl_pct_seen"),
            partial_taken=bool(sd.get("partial_taken")),
            runner_exit_profile=sd.get("runner_exit_profile"),
        )

        if buy_price and close_price and exit_policy.should_take_partial(subject, float(pnl_pct_live or 0.0)):
            fraction = float(exit_policy.partial_fraction(subject))
            remaining_before = float(sd.get("remaining_fraction") or 1.0)
            sell_fraction = min(max(0.05, fraction), remaining_before)
            if 0.0 < sell_fraction < remaining_before:
                pnl_ratio_partial = (float(close_price) - float(buy_price)) / float(buy_price)
                sd["realized_pnl_ratio"] = float(sd.get("realized_pnl_ratio") or 0.0) + sell_fraction * pnl_ratio_partial
                sd["remaining_fraction"] = max(0.0, remaining_before - sell_fraction)
                sd["partial_taken"] = True
                sd["partial_fraction"] = sell_fraction
                partial_age_s = _seconds_from_opened_at(opened, now)
                if partial_age_s is not None and sd.get("time_to_partial_sec") is None:
                    sd["time_to_partial_sec"] = partial_age_s
                sd["peak_after_partial_pct"] = max(
                    float(sd.get("peak_after_partial_pct") or 0.0),
                    float(pnl_ratio_partial * 100.0),
                )
                research_runtime.record_shadow_partial(
                    addr,
                    pnl_pct=float(pnl_ratio_partial * 100.0),
                    fraction_sold=float(sell_fraction),
                )
                subject.partial_taken = True

        exit_reason = exit_policy.should_exit(
            subject,
            close_price,
            now,
            liq_now=liq_now,
            pnl_pct=pnl_pct_live,
        )
        if exit_reason is None and close_price is None:
            age_h = (now - opened).total_seconds() / 3600.0
            if age_h < float(shadow_policy.max_holding_h):
                continue
            exit_reason = "TIMEOUT_NOPRICE"
        elif exit_reason is None:
            continue

        remaining_fraction = float(sd.get("remaining_fraction") or 1.0)
        realized_pnl_ratio = float(sd.get("realized_pnl_ratio") or 0.0)
        if buy_price and close_price:
            final_ratio = realized_pnl_ratio + remaining_fraction * ((float(close_price) - float(buy_price)) / float(buy_price))
            pnl_pct_total = float(final_ratio * 100.0)
            label = 1 if final_ratio >= ML_POSITIVE_PNL_RATIO else 0
        if pnl_pct_total is not None:
            sd["exit_from_peak_giveback_pct"] = max(0.0, float(sd.get("highest_pnl_pct") or 0.0) - float(pnl_pct_total))

        vec = sd.get("vec")
        if vec is not None:
            try:
                store_append(
                    vec,
                    label,
                    target_total_pnl_pct=pnl_pct_total,
                    sample_type="shadow_close",
                )
                _stats["appended_shadow"] += 1
            except Exception as exc:
                log.debug("store_append shadow %s → %s", addr[:6], exc)

        research_runtime.record_shadow_close(
            addr,
            regime=regime,
            pnl_pct=pnl_pct_total,
            exit_reason=str(exit_reason),
            label=label,
            close_price_usd=close_price,
            shadow_kind=str(sd.get("shadow_kind") or "execution"),
            extra={
                "reason": sd.get("reason"),
                "stage": sd.get("stage"),
                "ml_proba": sd.get("ml_proba"),
                "threshold": sd.get("threshold"),
                "rank_score": ((sd.get("rank_info") or {}).get("rank_score") if isinstance(sd.get("rank_info"), dict) else None),
                "partial_taken": bool(sd.get("partial_taken")),
                "runner_exit_profile": sd.get("runner_exit_profile"),
                "max_pnl_pct_seen": sd.get("max_pnl_pct_seen"),
                "time_to_partial_sec": sd.get("time_to_partial_sec"),
                "time_to_peak_sec": sd.get("time_to_peak_sec"),
                "peak_after_partial_pct": sd.get("peak_after_partial_pct"),
                "exit_from_peak_giveback_pct": sd.get("exit_from_peak_giveback_pct"),
            },
        )
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
    probe = await _probe_jupiter_route(output_mint, amount_sol)
    return probe.get("has_route")


async def _probe_jupiter_route(output_mint: str, amount_sol: float) -> Dict[str, Optional[float | bool]]:
    """Sonda ligera de ruta/impacto para enriquecer features y gates."""
    probe: Dict[str, Optional[float | bool]] = {
        "has_route": None,
        "price_impact_bps": None,
        "price_impact_pct": None,
    }
    try:
        if _JUP_ROUTER_AVAILABLE and jupiter is not None:
            SOL_MINT = "So11111111111111111111111111111111111111112"
            amt = max(0.005, min(float(amount_sol or 0.01), 0.2))
            q = await jupiter.get_quote(input_mint=SOL_MINT, output_mint=output_mint, amount_sol=amt)
            impact_bps = getattr(q, "price_impact_bps", None)
            impact_pct = None
            if isinstance(impact_bps, (int, float)):
                impact_pct = float(impact_bps) / 100.0
            probe = {
                "has_route": bool(getattr(q, "ok", False)),
                "price_impact_bps": float(impact_bps) if isinstance(impact_bps, (int, float)) else None,
                "price_impact_pct": impact_pct,
            }
    except Exception:
        probe = {"has_route": None, "price_impact_bps": None, "price_impact_pct": None}

    try:
        jpi = await jupiter_price.get_price(output_mint)
    except Exception:
        jpi = None

    if jpi is not None:
        if jpi.status == "OK":
            probe["has_route"] = True
        elif jpi.status == "NIL" and probe.get("has_route") is None:
            probe["has_route"] = False

    return probe


async def _maybe_apply_paper_sniper_liquidity_proxy(token: dict, addr: str) -> bool:
    if not (
        DRY_RUN
        and _PUMP_EARLY_SNIPER_ENABLED
        and _PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_ENABLED
    ):
        return False
    if token.get("liquidity_usd"):
        return False
    if str(token.get("entry_regime") or "").strip().lower() != "pump_early":
        return False
    if _candidate_age_minutes(token) < _PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_MIN_AGE_MIN:
        return False
    if not token.get("price_usd") or not token.get("market_cap_usd"):
        return False

    route_probe = await _probe_jupiter_route(
        addr,
        max(float(MIN_BUY_SOL), min(float(TRADE_AMOUNT_SOL_CFG), 0.05)),
    )
    if route_probe.get("has_route") is False:
        return False
    impact = route_probe.get("price_impact_pct")
    if impact is not None and float(impact) > _PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT:
        return False

    token["has_jupiter_route"] = int(route_probe.get("has_route") is not False)
    if impact is not None:
        token["price_impact_pct"] = float(impact)
    token["liquidity_usd"] = max(
        _PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_USD,
        _PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD,
    )
    token["liquidity_usd_is_proxy"] = 1
    token["sniper_liquidity_proxy"] = 1
    log.info(
        "Paper sniper liquidity proxy %s liq=%.0f route=%s impact=%s",
        addr[:6],
        float(token["liquidity_usd"]),
        route_probe.get("has_route"),
        f"{float(impact):.2f}%" if impact is not None else "?",
    )
    return True


async def _maybe_apply_green_sniper_liquidity_proxy(token: dict, addr: str) -> bool:
    if not (
        DRY_RUN
        and bool(getattr(CFG, "GREEN_SNIPER_ENABLED", True))
        and bool(getattr(CFG, "GREEN_SNIPER_PAPER_ROUTE_PROXY_ENABLED", True))
    ):
        return False
    if token.get("liquidity_usd"):
        return False
    if str(token.get("entry_regime") or "").strip().lower() != "pump_early":
        return False
    if not token.get("price_usd") and token.get("price_pct_5m") is None:
        return False
    min_proxy_liq = float(getattr(CFG, "GREEN_SNIPER_PAPER_ROUTE_PROXY_MIN_LIQUIDITY_USD", 1200.0) or 1200.0)
    token["liquidity_usd"] = min_proxy_liq
    token["liquidity_usd_is_proxy"] = 1
    token["liquidity_is_proxy"] = 1
    token["sniper_liquidity_proxy"] = 1
    token.setdefault("has_jupiter_route", 0)
    log.info("Green sniper paper liquidity proxy %s liq=%.0f", addr[:6], min_proxy_liq)
    return True


# ────────────────────────────────────────────────────────────────────────────
# run_bot.py  — _evaluate_and_buy
# ────────────────────────────────────────────────────────────────────────────
def _green_shadow_can_continue_to_runner_canary(token: dict, decision: object) -> bool:
    """Allow only selected real-liquidity Pump.fun shadows to reach rank/risk checks."""

    if not bool(getattr(CFG, "GREEN_SNIPER_RUNNER_CANARY_ENABLED", True)):
        return False
    if str(getattr(decision, "action", "") or "") != "shadow":
        return False
    if bool(getattr(decision, "paper_birth_probe", False)):
        return False

    failures = {str(item) for item in getattr(decision, "reject_reasons", ())}
    allowed_failures = {"low_green_momentum", "low_txns_5m", "weak_buy_sell_ratio"}
    if not failures or any(item not in allowed_failures for item in failures):
        return False

    source = str(token.get("discovered_via") or token.get("source") or "").strip().lower().replace("_", "")
    dex_id = _gate_dex_id(token)
    if source not in {"pumpfun", "pumpportal"} and dex_id != "pumpfun":
        return False
    if _is_liquidity_proxy(token):
        return False

    price_pct_5m = _metric_optional_float(token, "price_pct_5m")
    if price_pct_5m is None:
        return False
    min_price5m = float(getattr(CFG, "GREEN_SNIPER_RUNNER_CANARY_MIN_PRICE5M", 0.0) or 0.0)
    max_price5m = float(getattr(CFG, "GREEN_SNIPER_RUNNER_CANARY_MAX_PRICE5M", 90.0) or 90.0)
    if not (min_price5m <= float(price_pct_5m) < max_price5m):
        return False

    liquidity = _metric_float(token, "liquidity_usd")
    min_liq = float(getattr(CFG, "GREEN_SNIPER_RUNNER_CANARY_MIN_LIQUIDITY_USD", 1500.0) or 1500.0)
    if liquidity < min_liq:
        return False

    mcap = _metric_float(token, "market_cap_usd")
    max_mcap = float(getattr(CFG, "GREEN_SNIPER_RUNNER_CANARY_MAX_MARKET_CAP_USD", 25_000.0) or 25_000.0)
    if mcap <= 0 or mcap >= max_mcap:
        return False

    max_age = float(getattr(CFG, "GREEN_SNIPER_RUNNER_CANARY_MAX_AGE_MIN", 8.0) or 8.0)
    if _candidate_age_minutes(token) > max_age:
        return False

    max_impact = float(getattr(CFG, "GREEN_SNIPER_RUNNER_CANARY_MAX_PRICE_IMPACT_PCT", 20.0) or 20.0)
    if max(0.0, _metric_float(token, "price_impact_pct")) > max_impact:
        return False

    token["green_runner_canary_candidate"] = 1
    token["green_runner_canary_reason"] = ",".join(sorted(failures))
    return True


async def _evaluate_and_buy(token: dict, ses: SessionLocal) -> None:
    """Evalúa un token y, si pasa los filtros + IA, lanza la compra."""
    global _wallet_sol_balance

    token = sanitize_token_data(token)
    addr = token["address"]
    _stats["raw_discovered"] += 1
    if str(token.get("discovered_via") or "").strip().lower() == "pumpfun" and _stream_candidate_is_cooled(addr):
        return

    # 0) — gate horario (24/7 si no hay ventanas; BLOCK_HOURS siempre aplica si define) —
    if not _in_trading_window():
        delay = max(30, _delay_until_window())
        # Motivo de log diferenciado
        if _BLOCK_HOURS and _in_ranges(dt.datetime.now(), _BLOCK_HOURS):
            reason = "blocked_hour"
        else:
            reason = "off_hours"
        _requeue_with_stats(addr, reason=reason, backoff=delay)
        return

    # 1) — limpieza básica + log preliminar —
    require_jup_for_buy = filters.effective_require_jupiter_for_buy(token, _REQUIRE_JUP_FOR_BUY)
    queue_meta = lista_pares.meta(addr)
    meta = queue_meta or {}
    queue_attempts = int(meta.get("attempts", 0) or 0)
    first_seen_epoch_s = float(meta.get("first_seen", time.time()) or time.time())
    stored_discovered_via = str(meta.get("discovered_via") or "").strip().lower()
    if stored_discovered_via and not str(token.get("discovered_via") or "").strip():
        token["discovered_via"] = stored_discovered_via
    token.setdefault("discovered_via", "dex")
    stored_dex_id = _norm_dex_id(token.get("dex_id") or token.get("dexId") or meta.get("dex_id"))
    if stored_dex_id:
        token["dex_id"] = stored_dex_id
    if queue_meta is not None:
        _remember_queue_context(addr, token)
    token["queue_attempts"] = queue_attempts
    token["queue_age_minutes"] = max(0.0, (time.time() - first_seen_epoch_s) / 60.0)
    token["entry_regime"] = entry_sizing.classify_entry_regime(token, queue_attempts=queue_attempts)
    token["require_jupiter_for_buy"] = int(require_jup_for_buy)
    token["strategy_version"] = str(getattr(CFG, "SNIPER_STRATEGY_VERSION", "2026-04-green-sniper-v1") or "")
    token["experiment_id"] = str(getattr(CFG, "SNIPER_EXPERIMENT_ID", "green_v1") or "")
    if queue_attempts > 0 or _candidate_age_minutes(token) >= max(1.0, float(MIN_AGE_MIN)):
        warn_if_nulls(token, context=addr[:4])
    _log_token(token, addr)

    # 2) — duplicado: ya hay posición abierta —
    if await ses.scalar(select(Position).where(Position.address == addr,
                                               Position.closed.is_(False))):
        _remove_from_queue_if_present(addr)
        return

    # 3) — filtros inmediatos —
    if token.get("creator") in BANNED_CREATORS:
        _stats["filtered_out"] += 1
        _store_policy_reject(token, reason="banned_creator")
        _research_decision(token, action="rejected", reason="banned_creator", stage="early_filter", dedup_ttl_s=1800)
        _remove_from_queue_if_present(addr)
        return

    # ★ Pump.fun: intento rápido de precio con cuota/cooldown antes de requeue
    if token.get("discovered_via") == "pumpfun" and not token.get("liquidity_usd"):
        if _pf_can_try_now(addr):
            try:
                tok2 = await price_service.get_price(addr, use_gt=_PUMPFUN_PRICE_USE_GECKO, allow_partial=True)
                if tok2:
                    token.update(tok2)
                if token.get("liquidity_usd"):
                    pass  # ya tenemos liq/vol/mcap/price_usd
                elif await _maybe_apply_green_sniper_liquidity_proxy(token, addr):
                    pass
                elif await _maybe_apply_paper_sniper_liquidity_proxy(token, addr):
                    pass
                else:
                    _remember_stream_candidate_cooldown(addr, _PUMPFUN_STREAM_COOLDOWN_NO_LIQ_S)
                    return
            except Exception:
                _remember_stream_candidate_cooldown(addr, _PUMPFUN_STREAM_COOLDOWN_NO_LIQ_S)
                return
        else:
            _remember_stream_candidate_cooldown(addr, _PUMPFUN_STREAM_COOLDOWN_NO_LIQ_S)
            return

    # 4) — incomplete (sin liquidez) ---------------------------------
    if not token.get("liquidity_usd"):
        await _maybe_apply_green_sniper_liquidity_proxy(token, addr)

    if not token.get("liquidity_usd"):
        await _maybe_apply_paper_sniper_liquidity_proxy(token, addr)

    if not token.get("liquidity_usd"):
        # ⇢ solo contamos “incomplete” si el pool ya ha cumplido la edad mínima
        age_min_val = float(token.get("age_min") or 0.0)
        if age_min_val >= MIN_AGE_MIN:
            _stats["incomplete"] += 1

        token["is_incomplete"] = 1
        _store_policy_reject(token, reason="no_liq")
        _research_decision(token, action="rejected", reason="no_liq", stage="early_filter", dedup_ttl_s=1800)

        if token.get("discovered_via") == "pumpfun":
            _remember_stream_candidate_cooldown(addr, _PUMPFUN_STREAM_COOLDOWN_NO_LIQ_S)
            return

        attempts = int((meta := lista_pares.meta(addr) or {}).get("attempts", 0))
        backoff  = [60, 180, 420][min(attempts, 2)]
        backoff  = int(backoff * random.uniform(0.8, 1.2))  # jitter ±20%
        log.info("↩️  Re-queue %s (no_liq, intento %s)",
                 token.get("symbol") or addr[:4], attempts + 1)

        if attempts >= INCOMPLETE_RETRIES:
            _remove_from_queue_if_present(addr)
        else:
            _requeue_with_stats(addr, reason="no_liq", backoff=backoff, token=token)
        return

    # 5) — rellenar defaults y métricas opcionales —
    token = apply_default_values(token)
    token["is_incomplete"] = 0
    token["queue_attempts"] = queue_attempts
    token["queue_age_minutes"] = max(0.0, (time.time() - first_seen_epoch_s) / 60.0)
    token["entry_regime"] = entry_sizing.classify_entry_regime(token, queue_attempts=queue_attempts)
    token["require_jupiter_for_buy"] = int(require_jup_for_buy)

    # 6) — señales baratas (social, trend, insider…) —
    token["strategy_version"] = str(getattr(CFG, "SNIPER_STRATEGY_VERSION", "2026-04-green-sniper-v1") or "")
    token["experiment_id"] = str(getattr(CFG, "SNIPER_EXPERIMENT_ID", "green_v1") or "")
    green_decision = None
    if str(token.get("entry_regime") or "").strip().lower() == "pump_early" and bool(getattr(CFG, "GREEN_SNIPER_ENABLED", True)):
        fast = enrich_fast(token)
        token.update(fast.token)
        token.setdefault("score_total", filters.total_score(token))
        green_decision = evaluate_green_sniper(token, dry_run=DRY_RUN, live=not DRY_RUN)
        if green_decision.action in {"buy", "shadow", "delay"}:
            apply_green_sniper_context(token, green_decision)
            schedule_social_enrichment(token, lane=green_decision.lane)
        if green_decision.action == "delay":
            _research_decision(
                token,
                action="wait",
                reason=f"green_sniper:{green_decision.reason}",
                stage="green_sniper",
                dedup_ttl_s=30,
            )
            _requeue_with_stats(addr, reason=f"green_sniper:{green_decision.reason}", backoff=2, token=token)
            return
        green_shadow_canary_continue = _green_shadow_can_continue_to_runner_canary(token, green_decision)
        if green_decision.action == "shadow" and not green_shadow_canary_continue:
            vec_shadow = build_feature_vector(token)
            _research_decision(
                token,
                action="shadow",
                reason=f"green_sniper:{green_decision.reason}",
                stage="green_sniper",
                shadow_kind="green_sniper_reject_shadow",
                dedup_ttl_s=120,
            )
            await _open_shadow(
                addr,
                vec_shadow,
                price_hint=token.get("price_usd"),
                force=True,
                regime="pump_early",
                reason=f"green_sniper:{green_decision.reason}",
                stage="green_sniper",
                shadow_kind="green_sniper_reject_shadow",
            )
            _remember_stream_candidate_cooldown(addr, _stream_candidate_cooldown_s(token, "green_shadow"))
            _remove_from_queue_if_present(addr)
            return
        if green_decision.action == "shadow" and green_shadow_canary_continue:
            _research_decision(
                token,
                action="wait",
                reason=f"green_runner_canary_probe:{green_decision.reason}",
                stage="green_sniper",
                dedup_ttl_s=120,
            )
    green_fast_path = bool(green_decision and green_decision.action == "buy")
    if green_fast_path:
        require_jup_for_buy = bool(
            getattr(CFG, "GREEN_SNIPER_REQUIRE_ROUTE_LIVE", True)
            if not DRY_RUN
            else getattr(CFG, "GREEN_SNIPER_REQUIRE_ROUTE_PAPER", False)
        )
        token["require_jupiter_for_buy"] = int(require_jup_for_buy)

    if green_fast_path:
        token.setdefault("social_ok", None)
        token.setdefault("social_status", "unknown")
        token.setdefault("trend", None)
        token.setdefault("trend_fallback_used", True)
        token.setdefault("insider_sig", False)
        token["score_total"] = filters.total_score(token)
    else:
        token["social_ok"] = await socials.has_socials(addr)
    if not (green_fast_path and DRY_RUN and bool(getattr(CFG, "PAPER_SNIPER_MODE", False))):
        try:
            token["trend"], token["trend_fallback_used"] = await trend.trend_signal(addr)
        except trend.Trend404Retry:
            pass
        log.debug("⚠️  %s sin datos trend – continúa", addr[:4])
        if False:
            token["trend"] = None
            token["trend_fallback_used"] = True
        token.setdefault("trend", None)
        token.setdefault("trend_fallback_used", token.get("trend") is None)

        token["insider_sig"] = await insider.insider_alert(addr)
        token["score_total"] = filters.total_score(token)

    # 7) — filtro duro —
    if (not green_fast_path) and filters.basic_filters(token) is not True:
        attempts = int((meta := lista_pares.meta(addr) or {}).get("attempts", 0))
        keep, delay, reason = requeue_policy.decide(token, attempts,
                                                    meta.get("first_seen", time.time()))
        if keep:
            _research_decision(token, action="wait", reason=reason, stage="basic_filter", dedup_ttl_s=900)
            _requeue_with_stats(addr, reason=reason, backoff=delay, token=token)
        else:
            _stats["filtered_out"] += 1
            _store_policy_reject(token, reason="basic_filter")
            _research_decision(token, action="rejected", reason="basic_filter", stage="basic_filter", dedup_ttl_s=1800)
            _remember_stream_candidate_cooldown(addr, _stream_candidate_cooldown_s(token, "basic_filter"))
            _remove_from_queue_if_present(addr)
        return

    # 8) — señales caras —
    if green_fast_path and DRY_RUN and bool(getattr(CFG, "PAPER_SNIPER_MODE", False)):
        token.setdefault("rug_score", None)
        token.setdefault("cluster_bad", False)
    else:
        token["rug_score"]   = await rugcheck.check_token(addr)
        token["cluster_bad"] = await clusters.suspicious_cluster(addr)
    token["score_total"] = filters.total_score(token)

    quality_ok, quality_reason = (True, "") if green_fast_path else filters.snapshot_quality_gate(token)
    if not quality_ok:
        log.debug("🧱 Snapshot quality gate: %s (%s)", addr[:6], quality_reason or "blocked")
        _stats["filtered_out"] += 1
        vec = build_feature_vector(token)
        _store_policy_reject(vec, already_vector=True, reason=f"snapshot:{quality_reason or 'blocked'}")
        _research_decision(
            token,
            action="rejected",
            reason=f"snapshot:{quality_reason or 'blocked'}",
            stage="snapshot_quality",
            dedup_ttl_s=1800,
        )
        _requeue_or_cooldown_candidate(
            addr,
            token,
            reason=f"snapshot:{quality_reason or 'blocked'}",
            backoff=_DEX_MATURE_QUALITY_BACKOFF_S,
        )
        return

    route_probe = await _probe_jupiter_route(
        addr,
        max(MIN_BUY_SOL, min(float(TRADE_AMOUNT_SOL_CFG), 0.05)),
    )
    if route_probe.get("has_route") is not None:
        token["has_jupiter_route"] = int(bool(route_probe["has_route"]))
    if token.get("price_impact_pct") in (None, 0, 0.0) and route_probe.get("price_impact_pct") is not None:
        token["price_impact_pct"] = route_probe["price_impact_pct"]

    # 9) — IA + soft score gate —
    vec = build_feature_vector(token)
    vec_payload = vec.to_dict() if hasattr(vec, "to_dict") else dict(vec)
    for key in (
        "age_minutes",
        "snapshot_missing_fields",
        "coverage_core_fields",
        "entry_regime",
        "discovered_via",
        "price_source",
        "price_pct_5m",
        "txns_last_5m",
        "liquidity_is_proxy",
        "liquidity_usd_is_proxy",
        "mcap_bucket",
        "price5m_bucket",
        "venue_is_pumpswap",
        "impact_zero_flag",
    ):
        if key in vec_payload and vec_payload.get(key) is not None:
            token[key] = vec_payload.get(key)
    ml_gate_mode_raw = str(getattr(CFG, "ML_GATE_MODE", "legacy") or "legacy").strip().lower()
    proba = None if ml_gate_mode_raw == "off" else should_buy(vec)
    risk_proba = predict_risk(vec) if bool(getattr(CFG, "ML_RISK_MODEL_ENABLED", True)) else None
    ev_pred_pct = predict_ev(vec) if bool(getattr(CFG, "ML_EV_MODEL_ENABLED", True)) else None
    ai_threshold_eff = filters.effective_ai_threshold(token, AI_THRESHOLD)
    rank_info = research_runtime.score_candidate(vec_payload, proba=proba, threshold=ai_threshold_eff)
    _research_stage(
        token,
        stage="late_funnel",
        proba=proba,
        threshold=ai_threshold_eff,
        rank_info=rank_info,
    )
    if str(token.get("entry_regime") or "").strip().lower() == "pump_early" and _PUMP_EARLY_SNIPER_ENABLED:
        _tag_pump_sniper_gate(token, rank_info)
        vec = build_feature_vector(token)
        vec_payload = vec.to_dict() if hasattr(vec, "to_dict") else dict(vec)
        rank_info = research_runtime.score_candidate(vec_payload, proba=proba, threshold=ai_threshold_eff)
    research_canary_decision = evaluate_research_rank_canary(token, rank_info, dry_run=DRY_RUN, live=not DRY_RUN)
    research_rank_canary_fast_path = bool(research_canary_decision.allowed)
    if research_rank_canary_fast_path:
        apply_research_rank_canary_context(token, research_canary_decision)
        vec = build_feature_vector(token)
        vec_payload = vec.to_dict() if hasattr(vec, "to_dict") else dict(vec)
        rank_info = research_runtime.score_candidate(vec_payload, proba=proba, threshold=ai_threshold_eff)
    if str(token.get("entry_lane") or "").strip().lower() == "pump_early_green_candle_sniper":
        green_rank_guard = evaluate_green_sniper_rank_guard(rank_info)
        token["green_sniper_rank_score"] = float(green_rank_guard.rank_score)
        token["green_sniper_rank_guard_min_score"] = float(green_rank_guard.min_score)
        token["green_sniper_rank_guard_reason"] = str(green_rank_guard.reason)
        green_rank_bypass = False
        if green_rank_bypass:
            token["green_sniper_rank_guard_reason"] = "paper_birth_probe_bypass"
        if not green_rank_guard.allowed and not green_rank_bypass:
            _stats["filtered_out"] += 1
            reject_reason = f"green_rank_guard:{green_rank_guard.reason}"
            _store_policy_reject(vec, already_vector=True, reason=reject_reason)
            _research_decision(
                token,
                action="shadow",
                reason=reject_reason,
                stage="green_rank_guard",
                proba=proba,
                threshold=ai_threshold_eff,
                rank_info=rank_info,
                shadow_kind="green_sniper_reject_shadow",
            )
            await _open_shadow(
                addr,
                vec,
                price_hint=token.get("price_usd"),
                force=True,
                regime="pump_early",
                reason=reject_reason,
                stage="green_rank_guard",
                proba=proba,
                threshold=ai_threshold_eff,
                rank_info=rank_info,
                shadow_kind="green_sniper_reject_shadow",
            )
            _remember_stream_candidate_cooldown(addr, _stream_candidate_cooldown_s(token, "green_shadow"))
            _remove_from_queue_if_present(addr)
            return
        risk_guard = evaluate_green_sniper_risk_guard(token, dry_run=DRY_RUN, live=not DRY_RUN)
        token["green_sniper_risk_level"] = risk_guard.risk_level
        token["green_sniper_risk_reasons"] = ",".join(risk_guard.risk_reasons)
        token["green_sniper_risk_size_multiplier"] = float(risk_guard.size_multiplier)
        if not risk_guard.allow_buy:
            _stats["filtered_out"] += 1
            reject_reason = f"green_risk_guard:{risk_guard.risk_level}:{','.join(risk_guard.risk_reasons[:4])}"
            _store_policy_reject(vec, already_vector=True, reason=reject_reason)
            _research_decision(
                token,
                action="shadow",
                reason=reject_reason,
                stage="green_risk_guard",
                proba=proba,
                threshold=ai_threshold_eff,
                rank_info=rank_info,
                shadow_kind="green_sniper_reject_shadow",
            )
            if risk_guard.can_shadow:
                await _open_shadow(
                    addr,
                    vec,
                    price_hint=token.get("price_usd"),
                    force=True,
                    regime="pump_early",
                    reason=reject_reason,
                    stage="green_risk_guard",
                    proba=proba,
                    threshold=ai_threshold_eff,
                    rank_info=rank_info,
                    shadow_kind="green_sniper_reject_shadow",
                )
            _remember_stream_candidate_cooldown(addr, _stream_candidate_cooldown_s(token, "green_shadow"))
            _remove_from_queue_if_present(addr)
            return
    ml_gate = _ml_gate_state()
    ml_decision = decide_ml_action(
        token=token,
        feature_row=vec_payload,
        proba=proba,
        base_rules_passed=True,
        dry_run=DRY_RUN,
        live=not DRY_RUN,
        risk_proba=risk_proba,
        ev_pred_pct=ev_pred_pct,
    )
    if ml_decision.threshold is not None:
        ai_threshold_eff = float(ml_decision.threshold)
        rank_info = research_runtime.score_candidate(vec_payload, proba=proba, threshold=ai_threshold_eff)
    ml_pass = bool(proba is not None and proba >= ai_threshold_eff)
    log_ml_decision_event(
        addr,
        proba=float(proba or 0.0),
        threshold=float(ai_threshold_eff),
        passed=bool(ml_pass),
        enforced=bool(ml_decision.enforce),
        gate_mode=str(ml_decision.mode),
        activation_ready=ml_decision.activation_ready,
        discovered_via=str(token.get("discovered_via") or "dex"),
        entry_regime=str(token.get("entry_regime") or ""),
        score_total=_metric_int(token, "score_total"),
    )
    log_ml_policy_decision_event(addr, ml_decision, base_rules_passed=True)
    if not ml_decision.allow_buy:
        _stats["filtered_out"] += 1
        reject_reason = f"ml_policy:{ml_decision.reason}"
        _store_policy_reject(vec, already_vector=True, reason=reject_reason)
        _research_decision(
            token,
            action="rejected",
            reason=reject_reason,
            stage="ml_policy",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
        )
        await _maybe_open_research_shadow(
            token,
            vec,
            reason="ml_reject_shadow",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            stage="ml_policy",
        )
        _requeue_or_cooldown_candidate(
            addr,
            token,
            reason=reject_reason,
            backoff=_DEX_MATURE_QUALITY_BACKOFF_S,
        )
        return
    proba = float(proba or 0.0)

    soft_score_min_eff = filters.effective_soft_score_min(token, BUY_SOFT_SCORE_MIN)
    sniper_gate_ok_preview = bool(
        _PUMP_EARLY_SNIPER_ENABLED
        and str(token.get("entry_lane") or "").strip().lower()
        in {"pump_early_sniper", "pump_early_pumpswap_profit", "pump_early_green_candle_sniper", "pump_early_research_rank_canary", "pump_early_late_momentum_watch"}
        and _metric_int(token, "live_profit_gate_failed_count") == 0
    )
    if (
        not sniper_gate_ok_preview
        and soft_score_min_eff > 0
        and _metric_int(token, "score_total") < int(soft_score_min_eff)
    ):
        log.debug("🪫 Soft score gate: %s score_total=%d < %d",
                  addr[:6], _metric_int(token, "score_total"), soft_score_min_eff)
        _stats["filtered_out"] += 1
        _store_policy_reject(vec, already_vector=True, reason="soft_score")
        _research_decision(
            token,
            action="rejected",
            reason="soft_score",
            stage="soft_score",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
        )
        await _maybe_open_research_shadow(
            token,
            vec,
            reason="soft_score",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            soft_score_min=int(soft_score_min_eff),
            stage="soft_score",
        )
        _requeue_or_cooldown_candidate(
            addr,
            token,
            reason="soft_score",
            backoff=_DEX_MATURE_QUALITY_BACKOFF_S,
        )
        return

    _stats["ai_pass"] += 1

    size_decision = entry_sizing.compute_entry_sizing(
        token=token,
        ai_proba=proba,
        base_amount_sol=TRADE_AMOUNT_SOL_CFG,
        queue_attempts=queue_attempts,
        ai_threshold=ai_threshold_eff,
    )
    green_size_decision = None
    if str(token.get("entry_lane") or "").strip().lower() in {"pump_early_green_candle_sniper", "pump_early_late_momentum_watch"}:
        green_size_decision = compute_green_sniper_sizing(
            token,
            dry_run=DRY_RUN,
            live=not DRY_RUN,
            size_hint=token.get("green_sniper_size_hint"),
            risk_proba=risk_proba,
            ev_pred_pct=ev_pred_pct,
        )
        token["green_sniper_size_mode"] = green_size_decision.mode
        token["green_sniper_size_reason"] = green_size_decision.reason
    token["entry_regime"] = size_decision.regime
    strategy_decision = strategy_runtime.evaluate_candidate(
        token,
        regime=size_decision.regime,
        has_route=route_probe.get("has_route"),
    )
    green_paper_health_bypass = bool(
        (green_fast_path or research_rank_canary_fast_path)
        and DRY_RUN
        and bool(getattr(CFG, "PAPER_SNIPER_MODE", False))
        and bool(getattr(CFG, "PAPER_SNIPER_CONTINUE_ON_HEALTH", True))
    )
    log_strategy_decision_event(
        addr,
        regime=size_decision.regime,
        requested_mode=strategy_decision.requested_mode,
        effective_mode=strategy_decision.effective_mode,
        effective_execution_state=strategy_decision.effective_execution_state,
        action=strategy_decision.action,
        reason=strategy_decision.reason,
        confirmations=int(strategy_decision.confirmations),
        confirmations_required=int(strategy_decision.confirmations_required),
        health_state=strategy_decision.health_state,
        size_cap_multiplier=strategy_decision.size_cap_multiplier,
    )
    if strategy_decision.action == "wait" and not green_paper_health_bypass:
        log.info(
            "🧭 Strategy wait %s regime=%s reason=%s conf=%d/%d",
            addr[:6],
            size_decision.regime,
            strategy_decision.reason,
            strategy_decision.confirmations,
            strategy_decision.confirmations_required,
        )
        _research_decision(
            token,
            action="wait",
            reason=f"strategy:{strategy_decision.reason}",
            stage="strategy",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            dedup_ttl_s=max(300, int(strategy_decision.requeue_backoff_s)),
        )
        _ensure_requeue_with_stats(
            addr,
            reason=f"strategy:{strategy_decision.reason}",
            backoff=int(strategy_decision.requeue_backoff_s),
        )
        return
    if strategy_decision.action == "off" and not green_paper_health_bypass:
        _stats["filtered_out"] += 1
        _store_policy_reject(vec, already_vector=True, reason="strategy_off")
        _research_decision(
            token,
            action="rejected",
            reason="strategy_off",
            stage="strategy",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
        )
        _remove_from_queue_if_present(addr)
        return
    if strategy_decision.size_cap_multiplier is not None:
        size_decision = _apply_strategy_size_cap(size_decision, strategy_decision.size_cap_multiplier)
        token["entry_regime"] = size_decision.regime
    closed_trades_for_gate = None
    if DRY_RUN and _PAPER_COLD_START_ENABLED:
        closed_trades_for_gate = await _closed_position_count(ses)
    quality_ok_live, quality_reason_live = _entry_quality_gate(
        token,
        size_decision.regime,
        quality_points=size_decision.quality_points,
        rank_info=rank_info,
        paper_cold_start_active=_paper_cold_start_active(closed_trades_for_gate),
    )
    paper_shadow_probe_live = _paper_cold_start_shadow_probe_allowed(
        strategy_decision,
        size_decision,
        quality_ok_live,
        closed_trades_for_gate,
    )
    if paper_shadow_probe_live:
        size_decision = _apply_strategy_size_cap(
            size_decision,
            _PAPER_COLD_START_SHADOW_PROBE_SIZE_MULTIPLIER,
        )
        token["entry_regime"] = size_decision.regime
        token["paper_cold_start_shadow_probe"] = True
        token["paper_cold_start_shadow_probe_reason"] = str(strategy_decision.reason)
        log.info(
            "Paper cold-start recovery probe %s regime=%s reason=%s cap=%.2fx",
            addr[:6],
            size_decision.regime,
            strategy_decision.reason,
            size_decision.multiplier,
        )
    if strategy_decision.action == "live" and not quality_ok_live:
        log.info(
            "🧪 Entry quality wait %s regime=%s %s",
            addr[:6],
            size_decision.regime,
            quality_reason_live,
        )
        _stats["filtered_out"] += 1
        reject_reason = str(quality_reason_live or "entry_quality")
        _store_policy_reject(vec, already_vector=True, reason=reject_reason)
        _research_decision(
            token,
            action="rejected",
            reason=reject_reason,
            stage="entry_quality",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
        )
        await _maybe_open_research_shadow(
            token,
            vec,
            reason=reject_reason,
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            soft_score_min=int(soft_score_min_eff),
            stage="entry_quality",
        )
        _requeue_or_cooldown_candidate(
            addr,
            token,
            reason=reject_reason,
            backoff=_PUMP_EARLY_QUALITY_BACKOFF_S if size_decision.regime == "pump_early" else _DEX_MATURE_QUALITY_BACKOFF_S,
        )
        return
    if strategy_decision.action == "shadow" and not paper_shadow_probe_live and not green_paper_health_bypass:
        _research_decision(
            token,
            action="shadow",
            reason=f"strategy:{strategy_decision.reason}",
            stage="strategy",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            shadow_kind="execution",
        )
        await _open_shadow(
            addr,
            vec,
            price_hint=token.get("price_usd"),
            force=True,
            regime=size_decision.regime,
            reason=f"strategy:{strategy_decision.reason}",
            stage="strategy",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            shadow_kind="execution",
        )
        _remember_stream_candidate_cooldown(addr, _stream_candidate_cooldown_s(token, "shadow"))
        _remove_from_queue_if_present(addr)
        return

    if green_fast_path and not DRY_RUN:
        canary_ok, canary_reason = live_canary.evaluate_green_live_canary(token)
        if not canary_ok:
            _research_decision(
                token,
                action="shadow",
                reason=f"green_live_canary:{canary_reason}",
                stage="live_canary",
                proba=proba,
                threshold=ai_threshold_eff,
                rank_info=rank_info,
                shadow_kind="green_sniper_reject_shadow",
            )
            await _open_shadow(
                addr,
                vec,
                price_hint=token.get("price_usd"),
                force=True,
                regime=size_decision.regime,
                reason=f"green_live_canary:{canary_reason}",
                stage="live_canary",
                proba=proba,
                threshold=ai_threshold_eff,
                rank_info=rank_info,
                shadow_kind="green_sniper_reject_shadow",
            )
            _remove_from_queue_if_present(addr)
            return

    capacity_ok, regime_open, regime_cap = await _regime_capacity(ses, size_decision.regime)
    if not capacity_ok:
        log.info(
            "🧱 Exposure gate: %s regime=%s open=%d cap=%d",
            addr[:6],
            size_decision.regime,
            regime_open,
            regime_cap,
        )
        _research_decision(
            token,
            action="wait",
            reason=f"regime_cap:{size_decision.regime}",
            stage="capacity",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            dedup_ttl_s=600,
        )
        _requeue_with_stats(addr, reason=f"regime_cap:{size_decision.regime}", backoff=180, token=token)
        return

    lane_capacity_ok, lane_open, lane_cap = await _lane_capacity(ses, token.get("entry_lane"))
    if not lane_capacity_ok:
        lane = str(token.get("entry_lane") or "unknown")
        log.info(
            "Lane exposure gate: %s lane=%s open=%d cap=%d",
            addr[:6],
            lane,
            lane_open,
            lane_cap,
        )
        _research_decision(
            token,
            action="wait",
            reason=f"lane_cap:{lane}",
            stage="capacity",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            dedup_ttl_s=600,
        )
        _requeue_with_stats(addr, reason=f"lane_cap:{lane}", backoff=180, token=token)
        return

    # IMPORTANTE: no etiquetar 1 en T0; guardamos el vector para el cierre
    _pending_ai_vectors[addr] = vec

    log.debug(
        "📏 Entry sizing %s regime=%s bucket=%s mult=%.2fx amount=%.3f qpts=%d notes=%s",
        addr[:6],
        size_decision.regime,
        size_decision.bucket,
        size_decision.multiplier,
        size_decision.amount_sol,
        size_decision.quality_points,
        ",".join(size_decision.notes) or "-",
    )

    # 10) — importe —
    amount_sol = _compute_trade_amount(size_decision.multiplier)
    if green_size_decision is not None:
        amount_sol = float(green_size_decision.amount_sol)
    if research_rank_canary_fast_path:
        amount_sol = float(research_canary_decision.amount_sol)
    effective_min_buy_sol = float(getattr(CFG, "GREEN_SNIPER_LIVE_SIZE_SOL", MIN_BUY_SOL) or MIN_BUY_SOL) if green_fast_path else float(MIN_BUY_SOL)
    if amount_sol < effective_min_buy_sol:
        # Shadow si pasa IA pero no se compra por importe
        _research_decision(
            token,
            action="shadow",
            reason="min_buy_unavailable",
            stage="sizing",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            shadow_kind="execution",
        )
        await _open_shadow(
            addr,
            vec,
            price_hint=token.get("price_usd"),
            force=True,
            regime=size_decision.regime,
            reason="min_buy_unavailable",
            stage="sizing",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            shadow_kind="execution",
        )
        _remove_from_queue_if_present(addr)
        return

    # 11) — Persistir TOKEN (NaN→0.0 saneados) —
    try:
        token.setdefault("discovered_via", "dex")
        token.setdefault("discovered_at", utc_now())
        dex_id_norm = _norm_dex_id(token.get("dex_id") or token.get("dexId"))
        if dex_id_norm:
            token["dex_id"] = dex_id_norm
        token_db = prepare_token_for_db(token)
        valid_cols = {c.key for c in inspect(Token).mapper.column_attrs}
        await ses.merge(Token(**{k: v for k, v in token_db.items() if k in valid_cols}))
        await ses.commit()
    except SQLAlchemyError as exc:
        await ses.rollback()
        log.error("DB insert token %s → %s", addr[:4], exc)
        _pending_ai_vectors.pop(addr, None)
        _research_decision(
            token,
            action="rejected",
            reason="db_insert_error",
            stage="db",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            dedup_ttl_s=300,
        )
        _remove_from_queue_if_present(addr)
        return

    # 11.5) — Guard de pool (DEX whitelist) + ruta Jupiter (si router) —
    if REQUIRE_POOL_INITIALIZED:
        dex_id_norm = _norm_dex_id(token.get("dex_id") or token.get("dexId"))
        if dex_id_norm and DEX_WHITELIST and dex_id_norm not in DEX_WHITELIST:
            log.info("🛑 BUY bloqueado: DEX no whitelisted (dex=%s, allow=%s)", dex_id_norm, ",".join(DEX_WHITELIST))
            _stats["filtered_out"] += 1
            _pending_ai_vectors.pop(addr, None)
            _store_policy_reject(vec, already_vector=True, reason="dex_whitelist")
            _research_decision(
                token,
                action="rejected",
                reason="dex_whitelist",
                stage="execution_guard",
                proba=proba,
                threshold=ai_threshold_eff,
                rank_info=rank_info,
            )
            _remove_from_queue_if_present(addr)
            return

        # Mejor aún: comprobar ruta ejecutable (si router disponible)
        has_route = await _has_jupiter_route(addr, amount_sol)

        # ⚠️ Cambio clave: solo BLOQUEAMOS si la política exige Jupiter.
        if require_jup_for_buy:
            if has_route is False:
                log.info("🛑 BUY bloqueado: sin ruta Jupiter (mint=%s, reason=no_route)", addr[:6])
                _pending_ai_vectors.pop(addr, None)
                _research_decision(
                    token,
                    action="wait",
                    reason="no_route",
                    stage="execution_guard",
                    proba=proba,
                    threshold=ai_threshold_eff,
                    rank_info=rank_info,
                    dedup_ttl_s=900,
                )
                _requeue_with_stats(addr, reason="no_route", backoff=90, token=token)
                return
        else:
            # Data acquisition / DRY-RUN: seguimos aunque Jupiter aún no tenga ruta
            if has_route is False:
                log.debug("[run_bot] sin ruta Jupiter (mint=%s) pero require_jupiter_for_buy=false → continúo", addr[:6])
        # has_route is None → router no disponible: no bloqueamos aquí

    # 12) — “Exigir Jupiter” para comprar (solo precio) —
    if require_jup_for_buy:
        try:
            jtok = await price_service.get_price(addr, price_only=True)  # usa flag interno
        except Exception:
            jtok = None
        if not jtok or jtok.get("price_usd") in (None, 0):
            log.info("⏳ BUY aplazado (sin precio Jupiter) %s", addr[:6])
            _pending_ai_vectors.pop(addr, None)
            _research_decision(
                token,
                action="wait",
                reason="jupiter_price_missing",
                stage="execution_guard",
                proba=proba,
                threshold=ai_threshold_eff,
                rank_info=rank_info,
                dedup_ttl_s=900,
            )
            _requeue_or_cooldown_candidate(
                addr,
                token,
                reason="jupiter_price_missing",
                backoff=max(90, _DEX_MATURE_QUALITY_BACKOFF_S),
            )
            return

    if _runtime_buys_paused:
        log.info("BUY omitido por pause flag %s", addr[:6])
        _pending_ai_vectors.pop(addr, None)
        _research_decision(
            token,
            action="shadow",
            reason="buys_paused",
            stage="execution_guard",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            shadow_kind="execution",
            dedup_ttl_s=300,
        )
        await _open_shadow(
            addr,
            vec,
            price_hint=token.get("price_usd"),
            force=True,
            regime=size_decision.regime,
            reason="buys_paused",
            stage="execution_guard",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            shadow_kind="execution",
        )
        _remove_from_queue_if_present(addr)
        return

    # 12.5) — Rate limiter de BUY (no bloqueante): cooldown si no permite —
    if not _BUY_LIMITER.allow():
        cur = _BUY_LIMITER.current()
        log.info("⏳ BUY en cooldown por rate limit (%d/%ds, usado=%d)",
                 BUY_RATE_LIMIT_N, BUY_RATE_LIMIT_WINDOW_S, cur)
        # backoff prudente: mitad de ventana con jitter
        back = max(20, int(BUY_RATE_LIMIT_WINDOW_S * random.uniform(0.4, 0.7)))
        _pending_ai_vectors.pop(addr, None)
        _research_decision(
            token,
            action="wait",
            reason="buy_rate_limit",
            stage="execution_guard",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            dedup_ttl_s=300,
        )
        _requeue_with_stats(addr, reason="buy_rate_limit", backoff=back, token=token)
        return

    # 13) — BUY —
    try:
        if DRY_RUN:
            buy_resp = await buyer.buy(
                addr, amount_sol,
                price_hint=token.get("price_usd"),
                token_mint=token.get("address") or addr,
                liquidity_usd=token.get("liquidity_usd"),
                entry_regime=size_decision.regime,
                entry_lane=token.get("entry_lane"),
                discovered_via=token.get("discovered_via"),
                gate_profile=token.get("gate_profile") or token.get("sniper_gate_profile"),
                runner_exit_profile=token.get("runner_exit_profile"),
                exit_profile=token.get("exit_profile") or token.get("runner_exit_profile"),
                strategy_version=token.get("strategy_version"),
                experiment_id=token.get("experiment_id"),
                config_hash=_config_hash(),
            )
        else:
            buy_resp = await buyer.buy(
                addr,
                amount_sol,
                price_hint=token.get("price_usd"),
                token_mint=token.get("address") or addr,
                liquidity_usd=token.get("liquidity_usd"),
                entry_regime=size_decision.regime,
                entry_lane=token.get("entry_lane"),
                discovered_via=token.get("discovered_via"),
            )
    except Exception as exc:
        log.error("buyer.buy %s → %s", addr[:4], exc, exc_info=True)
        strategy_runtime.record_execution(size_decision.regime, False)
        log_execution_event(
            addr,
            regime=size_decision.regime,
            side="buy",
            ok=False,
            venue="exception",
        )
        # Shadow si falla la compra real
        _research_decision(
            token,
            action="shadow",
            reason="buy_exception",
            stage="execution",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            shadow_kind="execution",
            dedup_ttl_s=300,
        )
        await _open_shadow(
            addr,
            vec,
            price_hint=token.get("price_usd"),
            force=True,
            regime=size_decision.regime,
            reason="buy_exception",
            stage="execution",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            shadow_kind="execution",
        )
        _remove_from_queue_if_present(addr)
        return

    qty_lp = int(buy_resp.get("qty_lamports", 0) or 0)
    price_usd = buy_resp.get("buy_price_usd") or token.get("price_usd") or 0.0
    price_src = buy_resp.get("price_source")
    buy_sig = str(buy_resp.get("signature") or "")
    buy_venue = str(buy_resp.get("venue") or "")

    if qty_lp <= 0:
        strategy_runtime.record_execution(size_decision.regime, False)
        log_execution_event(
            addr,
            regime=size_decision.regime,
            side="buy",
            ok=False,
            venue=buy_venue or None,
            signature=buy_sig or None,
        )
        _research_decision(
            token,
            action="shadow",
            reason="buy_zero_qty",
            stage="execution",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            shadow_kind="execution",
            dedup_ttl_s=300,
        )
        await _open_shadow(
            addr,
            vec,
            price_hint=token.get("price_usd"),
            force=True,
            regime=size_decision.regime,
            reason="buy_zero_qty",
            stage="execution",
            proba=proba,
            threshold=ai_threshold_eff,
            rank_info=rank_info,
            shadow_kind="execution",
        )
        _remove_from_queue_if_present(addr)
        return

    strategy_runtime.record_execution(size_decision.regime, True)
    log_execution_event(
        addr,
        regime=size_decision.regime,
        side="buy",
        ok=True,
        venue=buy_venue or None,
        signature=buy_sig or None,
    )

    if not DRY_RUN:
        _wallet_sol_balance = max(_wallet_sol_balance - amount_sol, 0.0)
        if green_fast_path:
            live_canary.record_green_live_buy()

    token["runner_exit_profile"] = _runner_profile_for_subject(token)
    token["exit_profile"] = token.get("runner_exit_profile")
    token["config_hash"] = _config_hash()
    _research_decision(
        token,
        action="bought",
        reason="buy_ok",
        stage="execution",
        proba=proba,
        threshold=ai_threshold_eff,
        rank_info=rank_info,
        dedup_ttl_s=0,
    )

    # 14) — crear Position (incluye *buy_* métricas y fuente de compra) —
    runner_exit_profile = _runner_profile_for_subject(token)
    pos = Position(
        address=addr,
        symbol=token.get("symbol"),
        qty=qty_lp,
        entry_qty=qty_lp,
        buy_price_usd=price_usd,
        opened_at=utc_now(),
        highest_pnl_pct=0.0,
        max_pnl_pct_seen=0.0,
        entry_regime=size_decision.regime,
        size_bucket=size_decision.bucket,
        size_multiplier=float(size_decision.multiplier),
        buy_amount_sol=float(amount_sol),
        entry_notional_usd=float(buy_resp.get("entry_notional_usd") or 0.0),
        entry_ai_proba=float(proba),
        entry_score_total=_metric_int(token, "score_total"),
        entry_lane=str(token.get("entry_lane") or "") or None,
        gate_profile=str(token.get("gate_profile") or token.get("sniper_gate_profile") or "") or None,
        strategy_version=str(token.get("strategy_version") or "") or None,
        experiment_id=str(token.get("experiment_id") or "") or None,
        exit_profile=str(token.get("exit_profile") or runner_exit_profile or "") or None,
        config_hash=str(token.get("config_hash") or "") or None,
        buy_dex_id=str(token.get("dex_id") or token.get("dexId") or "") or None,
        buy_price_pct_5m=token.get("price_pct_5m"),
        buy_txns_last_5m=_metric_int(token, "txns_last_5m"),
        buy_liquidity_is_proxy=bool(_is_liquidity_proxy(token)),
        mcap_bucket=str(token.get("mcap_bucket") or "") or None,
        price5m_bucket=str(token.get("price5m_bucket") or "") or None,
        realized_qty=0,
        realized_proceeds_usd=0.0,
        realized_cost_usd=0.0,
        realized_pnl_usd=0.0,
        runner_exit_profile=runner_exit_profile,
        time_to_partial_sec=None,
        time_to_peak_sec=None,
        peak_after_partial_pct=None,
        exit_from_peak_giveback_pct=None,
        partial_count=0,
        buy_liquidity_usd=token.get("liquidity_usd"),
        buy_market_cap_usd=token.get("market_cap_usd"),
        buy_volume_24h_usd=token.get("volume_24h_usd"),
    )
    if hasattr(pos, "token_mint"):
        pos.token_mint = token.get("address") or addr
    if hasattr(pos, "price_source_at_buy"):
        pos.price_source_at_buy = price_src
    if hasattr(pos, "buy_tx_sig"):
        pos.buy_tx_sig = buy_sig or None

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
    meta = lista_pares.meta(addr) or {}
    _record_buy_stat()
    log_buy_event(
        addr,
        attempts=int(meta.get("attempts", 0) or 0),
        first_seen_epoch_s=float(meta["first_seen"]) if "first_seen" in meta else None,
        discovered_via=str(token.get("discovered_via") or "dex"),
        entry_regime=size_decision.regime,
        entry_lane=str(token.get("entry_lane") or ""),
        dex_id=str(token.get("dex_id") or token.get("dexId")) if (token.get("dex_id") or token.get("dexId")) else None,
        price_source_at_buy=str(price_src) if price_src else None,
        buy_amount_sol=float(amount_sol),
        size_multiplier=float(size_decision.multiplier),
        size_bucket=size_decision.bucket,
    )
    _remove_from_queue_if_present(addr)


async def _evaluate_and_buy_guarded(token: dict, ses: SessionLocal, *, source: str) -> None:
    addr = str((token or {}).get("address") or "???")
    try:
        if EVALUATE_TOKEN_TIMEOUT_S > 0:
            await asyncio.wait_for(_evaluate_and_buy(token, ses), timeout=EVALUATE_TOKEN_TIMEOUT_S)
        else:
            await _evaluate_and_buy(token, ses)
    except asyncio.TimeoutError:
        _note_runtime_error(f"eval_timeout:{source}[{addr[:8]}]", f">{EVALUATE_TOKEN_TIMEOUT_S:.0f}s")
        log.error("Eval %s %s timeout >%.0fs", source, addr[:6], EVALUATE_TOKEN_TIMEOUT_S)
    except Exception as exc:
        _note_runtime_error(f"eval_{source}[{addr[:8]}]", exc)
        log.error("Eval %s %s → %s", source, addr[:6], exc)


# ╭─────────────────────── Exit strategy (monitor) ───────────────────────────╮
async def _load_open_positions(ses: SessionLocal) -> Sequence[Position]:
    stmt = select(Position).where(Position.closed.is_(False))
    return (await ses.execute(stmt)).scalars().all()


async def _load_open_position_regimes(ses: SessionLocal) -> list[str]:
    stmt = (
        select(Position, Token.discovered_via)
        .outerjoin(Token, Position.address == Token.address)
        .where(Position.closed.is_(False))
    )
    rows = (await ses.execute(stmt)).all()
    regimes: list[str] = []
    for pos, discovered_via in rows:
        regime = getattr(pos, "entry_regime", None)
        if regime:
            regimes.append(str(regime))
            continue
        regimes.append(entry_sizing.classify_entry_regime({"discovered_via": discovered_via}))
    return regimes


async def _load_open_position_lanes(ses: SessionLocal) -> list[str]:
    stmt = select(Position.entry_lane, Position.gate_profile, Position.size_bucket).where(Position.closed.is_(False))
    rows = (await ses.execute(stmt)).all()
    lanes: list[str] = []
    for entry_lane, gate_profile, size_bucket in rows:
        lane = str(entry_lane or "").strip().lower()
        profile = str(gate_profile or "").strip().lower()
        bucket = str(size_bucket or "").strip().lower()
        if not lane and profile.startswith("pumpswap_breakout"):
            lane = "pump_early_pumpswap_breakout_probe"
        if not lane and bucket == "pumpswap_breakout":
            lane = "pump_early_pumpswap_breakout_probe"
        if not lane and (profile.startswith("green_sniper") or bucket.startswith("green_sniper")):
            lane = "pump_early_green_candle_sniper"
        if not lane and (profile.startswith("pumpswap_profit") or profile.startswith("pumpswap_meteor")):
            lane = "pump_early_pumpswap_profit"
        if not lane and bucket in {"pumpswap_profit", "pumpswap_prime", "pumpswap_meteor"}:
            lane = "pump_early_pumpswap_profit"
        if lane:
            lanes.append(lane)
    return lanes


async def _lane_capacity(ses: SessionLocal, entry_lane: str | None) -> tuple[bool, int, int]:
    lane = str(entry_lane or "").strip().lower()
    if lane not in {
        "pump_early_pumpswap_profit",
        "pump_early_pumpswap_breakout_probe",
        "pump_early_green_candle_sniper",
        "pump_early_research_rank_canary",
        "pump_early_late_momentum_watch",
    }:
        return True, 0, int(CFG.MAX_ACTIVE_POSITIONS)
    open_lanes = await _load_open_position_lanes(ses)
    if lane in {"pump_early_green_candle_sniper", "pump_early_research_rank_canary", "pump_early_late_momentum_watch"}:
        decision = evaluate_lane_position_limit(
            lane,
            [{"entry_lane": value} for value in open_lanes],
            dry_run=DRY_RUN,
            live=not DRY_RUN,
        )
        return decision.allowed, decision.open_count, min(int(CFG.MAX_ACTIVE_POSITIONS), max(1, int(decision.cap or 1)))
    current = sum(1 for value in open_lanes if value == lane)
    if lane == "pump_early_pumpswap_breakout_probe":
        limit = (
            _PUMP_EARLY_BREAKOUT_MAX_OPEN_PAPER
            if DRY_RUN
            else _PUMP_EARLY_BREAKOUT_MAX_OPEN_LIVE_CANARY
        )
    else:
        limit = (
            int(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_OPEN_PAPER", 2) or 2)
            if DRY_RUN
            else int(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_OPEN_LIVE_CANARY", 1) or 1)
        )
    limit = min(int(CFG.MAX_ACTIVE_POSITIONS), max(1, int(limit or 1)))
    return current < limit, current, limit


async def _regime_capacity(ses: SessionLocal, regime: str) -> tuple[bool, int, int]:
    open_regimes = await _load_open_position_regimes(ses)
    counts = entry_sizing.count_open_by_regime(open_regimes)
    current = int(counts.get(regime, 0))
    limit = int(entry_sizing.regime_position_cap(regime, CFG.MAX_ACTIVE_POSITIONS))
    if regime == "pump_early" and _PUMP_EARLY_SNIPER_ENABLED:
        if DRY_RUN:
            profit_cap = int(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_OPEN_PAPER", 2) or 2)
            breakout_cap = int(getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_OPEN_PAPER", 1) or 1)
            green_cap = int(getattr(CFG, "GREEN_SNIPER_MAX_OPEN_PAPER", getattr(CFG, "PUMP_EARLY_SNIPER_MAX_OPEN_PAPER", 6)) or 6)
            fallback_cap = int(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_OPEN_PAPER", 3) or 3)
            limit = min(
                int(CFG.MAX_ACTIVE_POSITIONS),
                max(
                    1,
                    (profit_cap + breakout_cap + green_cap)
                    if _PUMP_EARLY_PROFIT_LANE_ENABLED and _PUMP_EARLY_BREAKOUT_PROBE_ENABLED
                    else profit_cap
                    if _PUMP_EARLY_PROFIT_LANE_ENABLED
                    else fallback_cap,
                ),
            )
        else:
            health = (strategy_runtime.describe_regime_health().get("pump_early") or {})
            trade_count = int(health.get("trade_count") or 0)
            avg_pnl = health.get("avg_pnl_pct")
            severe = int(health.get("severe_exit_count") or 0)
            loss_streak = int(health.get("consecutive_losses") or 0)
            advanced_ready = (
                trade_count >= int(getattr(CFG, "PUMP_EARLY_SNIPER_ADVANCED_MIN_CLOSED", 10) or 10)
                and avg_pnl is not None
                and float(avg_pnl) >= float(getattr(CFG, "PUMP_EARLY_SNIPER_ADVANCED_MIN_AVG_PNL_PCT", 1.0) or 1.0)
                and severe <= 0
                and loss_streak <= int(getattr(CFG, "PUMP_EARLY_SNIPER_ADVANCED_MAX_LOSS_STREAK", 3) or 3)
            )
            live_cap = (
                int(getattr(CFG, "PUMP_EARLY_SNIPER_MAX_OPEN_LIVE_CANARY_ADVANCED", 2) or 2)
                if advanced_ready
                else int(
                    getattr(
                        CFG,
                        "PUMP_EARLY_PROFIT_MAX_OPEN_LIVE_CANARY"
                        if _PUMP_EARLY_PROFIT_LANE_ENABLED
                        else "PUMP_EARLY_SNIPER_MAX_OPEN_LIVE_CANARY",
                        1,
                    )
                    or 1
                )
            )
            if _PUMP_EARLY_PROFIT_LANE_ENABLED and _PUMP_EARLY_BREAKOUT_PROBE_ENABLED and not advanced_ready:
                live_cap += int(getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_OPEN_LIVE_CANARY", 1) or 1)
            if bool(getattr(CFG, "GREEN_SNIPER_LIVE_ENABLED", False)):
                live_cap += int(getattr(CFG, "GREEN_SNIPER_LIVE_MAX_OPEN", 1) or 1)
            limit = min(int(CFG.MAX_ACTIVE_POSITIONS), max(1, live_cap))
    return current < limit, current, limit


async def _should_exit(
    pos: Position,
    price: Optional[float],
    now: dt.datetime,
    *,
    liq_now: Optional[float] = None,
    pnl_pct: Optional[float] = None,
) -> Optional[str]:
    return exit_policy.should_exit(
        pos,
        price,
        now,
        liq_now=liq_now,
        pnl_pct=pnl_pct,
    )


def _position_execution_state(pos: Position) -> str:
    return "recovery" if str(getattr(pos, "size_bucket", "") or "").strip().lower() == "recovery" else "live"


def _position_health_metadata(pos: Position) -> dict[str, object]:
    return {
        "entry_lane": getattr(pos, "entry_lane", None),
        "dex_id": getattr(pos, "buy_dex_id", None),
        "liquidity_proxy_flag": bool(getattr(pos, "buy_liquidity_is_proxy", False)),
        "mcap_bucket": getattr(pos, "mcap_bucket", None),
        "price5m_bucket": getattr(pos, "price5m_bucket", None),
        "gate_profile": getattr(pos, "gate_profile", None),
    }


def _position_research_metrics(pos: Position) -> dict[str, object]:
    return {
        "runner_exit_profile": getattr(pos, "runner_exit_profile", None),
        "max_pnl_pct_seen": getattr(pos, "max_pnl_pct_seen", getattr(pos, "highest_pnl_pct", None)),
        "time_to_partial_sec": getattr(pos, "time_to_partial_sec", None),
        "time_to_peak_sec": getattr(pos, "time_to_peak_sec", None),
        "peak_after_partial_pct": getattr(pos, "peak_after_partial_pct", None),
        "exit_from_peak_giveback_pct": getattr(pos, "exit_from_peak_giveback_pct", None),
    }


def _entry_vector_for_close(vec: object, pos: Position) -> dict[str, object]:
    payload = vec.to_dict() if hasattr(vec, "to_dict") else dict(vec or {})
    if not payload.get("entry_lane"):
        payload["entry_lane"] = getattr(pos, "entry_lane", None)
    if not payload.get("gate_profile"):
        payload["gate_profile"] = getattr(pos, "gate_profile", None)
    if not payload.get("profit_lane_tier"):
        size_bucket = str(getattr(pos, "size_bucket", "") or "")
        if size_bucket == "pumpswap_meteor":
            payload["profit_lane_tier"] = "pump_early_meteor_prime"
        elif size_bucket == "pumpswap_breakout":
            payload["profit_lane_tier"] = "pump_early_pumpswap_breakout_probe"
        elif size_bucket.startswith("green_sniper"):
            payload["profit_lane_tier"] = "pump_early_green_candle_sniper"
        elif size_bucket == "pumpswap_prime":
            payload["profit_lane_tier"] = "pump_early_pumpswap_prime"
        elif str(getattr(pos, "entry_lane", "") or "") == "pump_early_pumpswap_profit":
            payload["profit_lane_tier"] = "pump_early_pumpswap_profit"
    if not payload.get("dex_id"):
        payload["dex_id"] = getattr(pos, "buy_dex_id", None)
    if payload.get("liquidity_is_proxy") is None:
        payload["liquidity_is_proxy"] = int(bool(getattr(pos, "buy_liquidity_is_proxy", False)))
    payload.setdefault("strategy_version", getattr(pos, "strategy_version", None))
    payload.setdefault("experiment_id", getattr(pos, "experiment_id", None))
    payload.setdefault("exit_profile", getattr(pos, "exit_profile", None) or getattr(pos, "runner_exit_profile", None))
    payload.setdefault("config_hash", getattr(pos, "config_hash", None))
    return payload


def _runner_profile_for_subject(subject: object) -> str | None:
    try:
        return exit_policy.resolve_runner_exit_profile(subject)
    except Exception:
        return None


def _config_hash() -> str:
    payload = {
        "strategy_version": str(getattr(CFG, "SNIPER_STRATEGY_VERSION", "")),
        "experiment_id": str(getattr(CFG, "SNIPER_EXPERIMENT_ID", "")),
        "green_enabled": bool(getattr(CFG, "GREEN_SNIPER_ENABLED", True)),
        "paper_sniper": bool(getattr(CFG, "PAPER_SNIPER_MODE", False)),
        "live_enabled": bool(getattr(CFG, "GREEN_SNIPER_LIVE_ENABLED", False)),
        "green_min_price5m": float(getattr(CFG, "GREEN_SNIPER_MIN_PRICE_PCT_5M", 20.0) or 20.0),
        "green_max_price5m": float(getattr(CFG, "GREEN_SNIPER_MAX_PRICE_PCT_5M", 280.0) or 280.0),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _seconds_from_opened_at(opened_at: object, now: dt.datetime) -> int | None:
    if not isinstance(opened_at, dt.datetime):
        return None
    opened = opened_at if opened_at.tzinfo is not None else opened_at.replace(tzinfo=dt.timezone.utc)
    current = now if now.tzinfo is not None else now.replace(tzinfo=dt.timezone.utc)
    return max(0, int((current - opened).total_seconds()))


def _update_position_peak_metrics(
    pos: Position,
    *,
    pnl_pct: float,
    price_usd: float | None,
    observed_at: dt.datetime,
) -> bool:
    changed = False
    peak_before = float(getattr(pos, "highest_pnl_pct", 0.0) or 0.0)
    if float(pnl_pct) <= peak_before:
        if hasattr(pos, "max_pnl_pct_seen"):
            pos.max_pnl_pct_seen = peak_before
        return False

    pos.highest_pnl_pct = float(pnl_pct)
    if hasattr(pos, "max_pnl_pct_seen"):
        pos.max_pnl_pct_seen = float(pnl_pct)
    if price_usd is not None:
        if hasattr(pos, "peak_price_usd"):
            pos.peak_price_usd = float(price_usd)
        if hasattr(pos, "peak_price"):
            pos.peak_price = float(price_usd)
    peak_age_s = _seconds_from_opened_at(getattr(pos, "opened_at", None), observed_at)
    if peak_age_s is not None and hasattr(pos, "time_to_peak_sec"):
        current_peak_s = getattr(pos, "time_to_peak_sec", None)
        if current_peak_s is None or peak_age_s < int(current_peak_s):
            pos.time_to_peak_sec = peak_age_s
    if bool(getattr(pos, "partial_taken", False)) and hasattr(pos, "peak_after_partial_pct"):
        prev_peak_after_partial = getattr(pos, "peak_after_partial_pct", None)
        prev_peak_after_partial_f = float(prev_peak_after_partial or 0.0) if prev_peak_after_partial is not None else 0.0
        if float(pnl_pct) > prev_peak_after_partial_f:
            pos.peak_after_partial_pct = float(pnl_pct)
    changed = True
    return changed


def _finalize_position_runner_metrics(pos: Position) -> None:
    peak_pct = float(getattr(pos, "highest_pnl_pct", 0.0) or 0.0)
    if hasattr(pos, "max_pnl_pct_seen"):
        pos.max_pnl_pct_seen = peak_pct
    total_pnl_pct = getattr(pos, "total_pnl_pct", None)
    try:
        total_pnl_pct_f = float(total_pnl_pct) if total_pnl_pct is not None else None
    except Exception:
        total_pnl_pct_f = None
    if total_pnl_pct_f is not None and hasattr(pos, "exit_from_peak_giveback_pct"):
        pos.exit_from_peak_giveback_pct = max(0.0, peak_pct - total_pnl_pct_f)


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
        vec = _entry_vector_for_close(vec, pos)
        pnl_ratio = total_pnl_ratio_from_record(
            pos,
            close_price_usd=price_used if price_used is not None else getattr(pos, "close_price_usd", None),
        )
        label = 1 if pnl_ratio >= float(ML_POSITIVE_PNL_RATIO) else 0
        store_append(
            vec,
            label,
            target_total_pnl_pct=float(pnl_ratio * 100.0),
            sample_type="trade_close",
        )
        _stats["appended_at_close"] += 1
    except Exception as exc:
        log.debug("persist_dataset_at_close %s → %s", pos.address[:6], exc)


# ────────────────────────── Monitor de posiciones ───────────────────────────
def _record_partial_trade_fill(
    pos: Position,
    *,
    qty_sold: int,
    fill_price_usd: Optional[float],
    filled_at: dt.datetime,
) -> None:
    remaining_before = int(getattr(pos, "qty", 0) or 0)
    sold_qty = max(0, min(remaining_before, int(qty_sold or 0)))
    fill_px = float(fill_price_usd if fill_price_usd is not None else (getattr(pos, "buy_price_usd", 0.0) or 0.0))

    totals = apply_partial_fill(
        entry_qty=getattr(pos, "entry_qty", 0),
        remaining_qty=remaining_before,
        buy_price_usd=getattr(pos, "buy_price_usd", 0.0),
        entry_notional_usd=getattr(pos, "entry_notional_usd", None),
        realized_qty=getattr(pos, "realized_qty", 0),
        realized_proceeds_usd=getattr(pos, "realized_proceeds_usd", 0.0),
        qty_sold=sold_qty,
        fill_price_usd=fill_px,
    )

    pos.qty = int(totals.remaining_qty)
    if hasattr(pos, "entry_qty"):
        pos.entry_qty = int(totals.entry_qty)
    if hasattr(pos, "realized_qty"):
        pos.realized_qty = int(totals.realized_qty)
    if hasattr(pos, "realized_proceeds_usd"):
        pos.realized_proceeds_usd = float(totals.realized_proceeds_usd)
    if hasattr(pos, "realized_cost_usd"):
        pos.realized_cost_usd = float(totals.realized_cost_usd)
    if hasattr(pos, "realized_pnl_usd"):
        pos.realized_pnl_usd = float(totals.realized_pnl_usd)
    if hasattr(pos, "partial_taken"):
        pos.partial_taken = True
    if hasattr(pos, "partial_count"):
        pos.partial_count = int(getattr(pos, "partial_count", 0) or 0) + 1
    if hasattr(pos, "first_partial_at") and getattr(pos, "first_partial_at", None) is None:
        pos.first_partial_at = filled_at
    if hasattr(pos, "last_partial_at"):
        pos.last_partial_at = filled_at
    if hasattr(pos, "last_partial_qty"):
        pos.last_partial_qty = sold_qty
    if hasattr(pos, "last_partial_price_usd"):
        pos.last_partial_price_usd = fill_px
    partial_age_s = _seconds_from_opened_at(getattr(pos, "opened_at", None), filled_at)
    if partial_age_s is not None and hasattr(pos, "time_to_partial_sec") and getattr(pos, "time_to_partial_sec", None) is None:
        pos.time_to_partial_sec = partial_age_s
    if hasattr(pos, "peak_after_partial_pct"):
        pos.peak_after_partial_pct = max(
            float(getattr(pos, "peak_after_partial_pct", 0.0) or 0.0),
            float(getattr(pos, "highest_pnl_pct", 0.0) or 0.0),
        )


def _refresh_position_trade_metrics(pos: Position) -> bool:
    before = (
        int(getattr(pos, "entry_qty", 0) or 0),
        float(getattr(pos, "realized_cost_usd", 0.0) or 0.0),
        float(getattr(pos, "realized_pnl_usd", 0.0) or 0.0),
    )
    totals = summarize_trade(
        entry_qty=getattr(pos, "entry_qty", 0),
        remaining_qty=int(getattr(pos, "qty", 0) or 0),
        buy_price_usd=getattr(pos, "buy_price_usd", 0.0),
        entry_notional_usd=getattr(pos, "entry_notional_usd", None),
        realized_qty=getattr(pos, "realized_qty", 0),
        realized_proceeds_usd=getattr(pos, "realized_proceeds_usd", 0.0),
        close_price_usd=None,
    )
    if hasattr(pos, "entry_qty"):
        pos.entry_qty = int(totals.entry_qty)
    if hasattr(pos, "realized_cost_usd"):
        pos.realized_cost_usd = float(totals.realized_cost_usd)
    if hasattr(pos, "realized_pnl_usd"):
        pos.realized_pnl_usd = float(totals.realized_pnl_usd)
    after = (
        int(getattr(pos, "entry_qty", 0) or 0),
        float(getattr(pos, "realized_cost_usd", 0.0) or 0.0),
        float(getattr(pos, "realized_pnl_usd", 0.0) or 0.0),
    )
    return after != before


def _seal_closed_trade_metrics(pos: Position, close_price_usd: Optional[float]) -> None:
    close_px = float(close_price_usd if close_price_usd is not None else (getattr(pos, "close_price_usd", 0.0) or 0.0))
    remaining_qty = int(getattr(pos, "qty", 0) or 0)
    totals = summarize_trade(
        entry_qty=getattr(pos, "entry_qty", 0),
        remaining_qty=remaining_qty,
        buy_price_usd=getattr(pos, "buy_price_usd", 0.0),
        entry_notional_usd=getattr(pos, "entry_notional_usd", None),
        realized_qty=getattr(pos, "realized_qty", 0),
        realized_proceeds_usd=getattr(pos, "realized_proceeds_usd", 0.0),
        close_price_usd=close_px,
    )

    if hasattr(pos, "entry_qty"):
        pos.entry_qty = int(totals.entry_qty)
    if hasattr(pos, "realized_cost_usd"):
        pos.realized_cost_usd = float(totals.realized_cost_usd)
    if hasattr(pos, "realized_pnl_usd"):
        pos.realized_pnl_usd = float(totals.realized_pnl_usd)
    if hasattr(pos, "effective_exit_price_usd"):
        pos.effective_exit_price_usd = totals.effective_exit_price_usd
    if hasattr(pos, "total_pnl_usd"):
        pos.total_pnl_usd = float(totals.total_pnl_usd)
    if hasattr(pos, "total_pnl_pct"):
        pos.total_pnl_pct = float(totals.total_pnl_pct)
    _finalize_position_runner_metrics(pos)
    pos.qty = 0


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
        await _ensure_position_entry_notional(pos, ses)
        mint_key = getattr(pos, "token_mint", None) or pos.address
        pos_regime = str(getattr(pos, "entry_regime", None) or exit_policy.resolve_entry_regime(pos))

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

        strategy_runtime.record_monitor_coverage(pos_regime, price is not None)

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

        # ── Actualizar pnl_pct + peak (si hay precio) ───────────────────
        pnl_pct: Optional[float] = None
        if price is not None and pos.buy_price_usd:
            try:
                pnl_pct = (float(price) - float(pos.buy_price_usd)) / float(pos.buy_price_usd) * 100.0
                if _update_position_peak_metrics(pos, pnl_pct=float(pnl_pct), price_usd=float(price), observed_at=now):
                    try:
                        await ses.commit()
                    except Exception:
                        try:
                            await ses.rollback()
                        except Exception:
                            pass
            except Exception:
                pnl_pct = None

        pos_exit_policy = exit_policy.effective_exit_policy(pos)
        if (
            getattr(pos_exit_policy, "runner_exit_profile", None)
            and getattr(pos, "runner_exit_profile", None) != pos_exit_policy.runner_exit_profile
        ):
            pos.runner_exit_profile = pos_exit_policy.runner_exit_profile
            try:
                await ses.commit()
            except Exception:
                try:
                    await ses.rollback()
                except Exception:
                    pass

        # ── Liquidity crush inmediato (manteniendo tu comportamiento) ────
        if (
            getattr(pos, "buy_liquidity_usd", None)
            and liq_now
            and not (bool(getattr(pos, "partial_taken", False)) and price is not None)
            and float(pos_exit_policy.liq_crush_fraction) > 0
            and float(liq_now) <= float(pos.buy_liquidity_usd) * float(pos_exit_policy.liq_crush_fraction)
        ):
            sell_resp = await seller.sell(
                pos.address,
                pos.qty,
                token_mint=mint_key,
                price_hint=price,
                price_source_hint=price_src,
            )

            # Si falla la venta, NO cierres (evita “closed=true” sin haber vendido)
            if sell_resp is not None and sell_resp.get("ok") is False:
                log.warning("⚠️ SELL LIQ_CRUSH falló %s: %s", pos.address[:6], sell_resp.get("err"))
                strategy_runtime.record_execution(pos_regime, False)
                log_execution_event(
                    pos.address,
                    regime=pos_regime,
                    side="sell",
                    ok=False,
                    venue=str((sell_resp or {}).get("venue") or "") or None,
                    signature=str((sell_resp or {}).get("signature") or "") or None,
                )
                try:
                    await ses.rollback()
                except Exception:
                    pass
                continue

            strategy_runtime.record_execution(pos_regime, True)
            log_execution_event(
                pos.address,
                regime=pos_regime,
                side="sell",
                ok=True,
                venue=str((sell_resp or {}).get("venue") or "") or None,
                signature=str((sell_resp or {}).get("signature") or "") or None,
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
            if hasattr(pos, "exit_tx_sig"):
                pos.exit_tx_sig = (sell_resp or {}).get("signature")

            _seal_closed_trade_metrics(pos, pos.close_price_usd)

            try:
                await ses.commit()
            except SQLAlchemyError:
                await ses.rollback()

            # Persistencia dataset al cierre
            _persist_dataset_at_close(pos, used_close if used_close is not None else price)
            research_runtime.record_live_trade_close(
                pos.address,
                regime=pos_regime,
                pnl_pct=getattr(pos, "total_pnl_pct", None),
                exit_reason="LIQUIDITY_CRUSH",
                extra={
                    "price_source_at_close": getattr(pos, "price_source_at_close", None),
                    "close_price_usd": getattr(pos, "close_price_usd", None),
                    **_position_health_metadata(pos),
                    **_position_research_metrics(pos),
                },
            )
            strategy_runtime.record_trade_close(
                pos_regime,
                getattr(pos, "total_pnl_pct", None),
                exit_reason="LIQUIDITY_CRUSH",
                execution_state=_position_execution_state(pos),
                **_position_health_metadata(pos),
            )

            # ✅ Fix: NO sumar “a ojo”. Refresco balance real tras trade.
            if not DRY_RUN:
                await _refresh_balance_force("after_sell_liq_crush")

            _record_sell_stat(now)
            sells_done += 1
            continue  # siguiente posición

        # ── TP parcial único al tocar TP (antes de decidir salida total) ──
        if (
            pos_exit_policy.tp_partial_enabled
            and price is not None
            and pnl_pct is not None
            and exit_policy.should_take_partial(pos, pnl_pct)
        ):
            qty_total = int(getattr(pos, "qty", 0) or 0)
            # si no hay margen, no hacemos parcial → dejamos que el exit TP cierre total si toca
            if qty_total > 0:
                qty_to_sell = int(round(qty_total * float(pos_exit_policy.tp_partial_fraction)))
                qty_to_sell = max(1, qty_to_sell)

                # asegurar remanente mínimo si se puede
                if TP_PARTIAL_MIN_REMAIN_LAMPORTS > 0 and (qty_total - qty_to_sell) < TP_PARTIAL_MIN_REMAIN_LAMPORTS:
                    qty_to_sell = max(1, qty_total - TP_PARTIAL_MIN_REMAIN_LAMPORTS)

                # si la venta parcial se come todo, mejor no hacer parcial (dejamos cierre total por TP)
                if qty_to_sell >= qty_total:
                    qty_to_sell = 0

                if qty_to_sell > 0:
                    log.info(
                        "💰 TP parcial %s pnl=%s%% → vendiendo %d/%d (%.0f%%)",
                        (pos.symbol or pos.address[:6]),
                        _fmt(pnl_pct, "{:.1f}"),
                        qty_to_sell,
                        qty_total,
                        pos_exit_policy.tp_partial_fraction * 100.0,
                    )
                    part_resp = await seller.sell(
                        pos.address,
                        qty_to_sell,
                        token_mint=mint_key,
                        price_hint=price,
                        price_source_hint=price_src,
                    )

                    if part_resp is not None and part_resp.get("ok") is False:
                        log.warning("⚠️ TP parcial falló %s: %s", pos.address[:6], part_resp.get("err"))
                        strategy_runtime.record_execution(pos_regime, False)
                        log_execution_event(
                            pos.address,
                            regime=pos_regime,
                            side="sell_partial",
                            ok=False,
                            venue=str((part_resp or {}).get("venue") or "") or None,
                            signature=str((part_resp or {}).get("signature") or "") or None,
                        )
                        try:
                            await ses.rollback()
                        except Exception:
                            pass
                        # no cierres en este tick; reintenta luego
                        continue

                    strategy_runtime.record_execution(pos_regime, True)
                    log_execution_event(
                        pos.address,
                        regime=pos_regime,
                        side="sell_partial",
                        ok=True,
                        venue=str((part_resp or {}).get("venue") or "") or None,
                        signature=str((part_resp or {}).get("signature") or "") or None,
                    )

                    part_price_used = (part_resp or {}).get("price_used_usd")
                    part_qty_sold = int((part_resp or {}).get("qty_sold") or qty_to_sell)
                    _record_partial_trade_fill(
                        pos,
                        qty_sold=part_qty_sold,
                        fill_price_usd=part_price_used if part_price_used is not None else price,
                        filled_at=now,
                    )

                    try:
                        await ses.commit()
                    except Exception:
                        try:
                            await ses.rollback()
                        except Exception:
                            pass

                    # refresco real (solo modo real)
                    if not DRY_RUN:
                        await _refresh_balance_force("after_partial_tp")

                    # si por cualquier razón quedó a 0, lo tratamos como cierre total
                    if int(getattr(pos, "qty", 0) or 0) <= 0:
                        pos.closed = True
                        pos.closed_at = now
                        pos.exit_reason = "TAKE_PROFIT"
                        try:
                            pos.close_price_usd = float(part_price_used) if part_price_used is not None else (float(price) if price is not None else pos.buy_price_usd)
                        except Exception:
                            pos.close_price_usd = pos.buy_price_usd
                        if hasattr(pos, "price_source_at_close"):
                            pos.price_source_at_close = (part_resp or {}).get("price_source_close") or price_src or None
                        if hasattr(pos, "exit_tx_sig"):
                            pos.exit_tx_sig = (part_resp or {}).get("signature")
                        _seal_closed_trade_metrics(pos, pos.close_price_usd)
                        try:
                            await ses.commit()
                        except Exception:
                            try:
                                await ses.rollback()
                            except Exception:
                                pass
                        if DRY_RUN:
                            try:
                                refresh_post_partial_experiment_snapshot()
                            except Exception:
                                log.exception("post-partial experiment snapshot refresh failed")
                        _persist_dataset_at_close(pos, price)
                        research_runtime.record_live_trade_close(
                            pos.address,
                            regime=pos_regime,
                            pnl_pct=getattr(pos, "total_pnl_pct", None),
                            exit_reason="TAKE_PROFIT",
                            extra={
                                "price_source_at_close": getattr(pos, "price_source_at_close", None),
                                "close_price_usd": getattr(pos, "close_price_usd", None),
                                **_position_health_metadata(pos),
                                **_position_research_metrics(pos),
                            },
                        )
                        strategy_runtime.record_trade_close(
                            pos_regime,
                            getattr(pos, "total_pnl_pct", None),
                            exit_reason="TAKE_PROFIT",
                            execution_state=_position_execution_state(pos),
                            **_position_health_metadata(pos),
                        )
                        _record_sell_stat(now)
                        sells_done += 1
                    continue  # tras parcial, NO cierres en este tick

        # ③ Evaluar salida con el precio disponible (puede ser None)
        exit_reason = await _should_exit(pos, price, now, liq_now=liq_now, pnl_pct=pnl_pct)
        if exit_reason is None:
            continue

        # ④ SELL — seller.sell hará su propio cálculo robusto de precio
        sell_resp = await seller.sell(
            pos.address,
            pos.qty,
            token_mint=mint_key,
            price_hint=price,            # el que calculaste en el monitor (puede ser None)
            price_source_hint=price_src, # "jup_batch" | "jup_single" | "jup_critical" | "dex_full" | None
        )

        # Si falla la venta, NO cierres la posición
        if sell_resp is not None and sell_resp.get("ok") is False:
            log.warning("⚠️ SELL falló %s (%s): %s", pos.address[:6], exit_reason, sell_resp.get("err"))
            strategy_runtime.record_execution(pos_regime, False)
            log_execution_event(
                pos.address,
                regime=pos_regime,
                side="sell",
                ok=False,
                venue=str((sell_resp or {}).get("venue") or "") or None,
                signature=str((sell_resp or {}).get("signature") or "") or None,
            )
            try:
                await ses.rollback()
            except Exception:
                pass
            continue

        strategy_runtime.record_execution(pos_regime, True)
        log_execution_event(
            pos.address,
            regime=pos_regime,
            side="sell",
            ok=True,
            venue=str((sell_resp or {}).get("venue") or "") or None,
            signature=str((sell_resp or {}).get("signature") or "") or None,
        )

        pos.closed = True
        pos.closed_at = now
        pos.exit_reason = str(exit_reason)[:24]

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
        _seal_closed_trade_metrics(pos, pos.close_price_usd)

        _record_sell_stat(now)
        sells_done += 1

        try:
            await ses.commit()
        except SQLAlchemyError:
            await ses.rollback()
        else:
            if DRY_RUN:
                try:
                    refresh_post_partial_experiment_snapshot()
                except Exception:
                    log.exception("post-partial experiment snapshot refresh failed")

        # Persistencia dataset al cierre
        _persist_dataset_at_close(pos, used_close if used_close is not None else price)
        research_runtime.record_live_trade_close(
            pos.address,
            regime=pos_regime,
            pnl_pct=getattr(pos, "total_pnl_pct", None),
            exit_reason=str(exit_reason),
            extra={
                "price_source_at_close": getattr(pos, "price_source_at_close", None),
                "close_price_usd": getattr(pos, "close_price_usd", None),
                **_position_health_metadata(pos),
                **_position_research_metrics(pos),
            },
        )
        strategy_runtime.record_trade_close(
            pos_regime,
            getattr(pos, "total_pnl_pct", None),
            exit_reason=str(exit_reason),
            execution_state=_position_execution_state(pos),
            **_position_health_metadata(pos),
        )
        if (not DRY_RUN) and str(getattr(pos, "entry_lane", "") or "").strip().lower() == "pump_early_green_candle_sniper":
            try:
                sol_usd = float(await get_sol_usd())
            except Exception:
                sol_usd = 1.0
            live_canary.record_green_live_close(
                pnl_sol=float(getattr(pos, "total_pnl_usd", 0.0) or 0.0) / max(sol_usd, 1.0),
                exit_reason=str(exit_reason),
            )

        # ✅ Fix: NO sumar “a ojo”. Refresco balance real tras trade.
        if not DRY_RUN:
            await _refresh_balance_force("after_sell")

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


async def _bootstrap_strategy_runtime(ses: SessionLocal) -> None:
    def _hist_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            parsed = float(value)
            if parsed != parsed or parsed in (float("inf"), float("-inf")):
                return None
            return parsed
        except Exception:
            return None

    def _historical_breakout_matches(
        *,
        buy_dex_id: object,
        buy_liquidity_is_proxy: object,
        buy_liquidity_usd: object,
        buy_market_cap_usd: object,
        buy_volume_24h_usd: object,
        buy_price_pct_5m: object,
        buy_txns_last_5m: object,
        entry_score_total: object,
    ) -> bool:
        if not _PUMP_EARLY_BREAKOUT_PROBE_ENABLED:
            return False
        dex_id = str(buy_dex_id or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")
        liquidity = _hist_float(buy_liquidity_usd)
        mcap = _hist_float(buy_market_cap_usd)
        volume_24h = _hist_float(buy_volume_24h_usd)
        price_pct_5m = _hist_float(buy_price_pct_5m)
        txns_5m = int(_hist_float(buy_txns_last_5m) or 0)
        score_total = int(_hist_float(entry_score_total) or 0)
        liq_proxy = bool(buy_liquidity_is_proxy)
        return (
            dex_id == "pumpswap"
            and not liq_proxy
            and liquidity is not None
            and _PUMP_EARLY_BREAKOUT_MIN_LIQUIDITY_USD <= liquidity <= _PUMP_EARLY_BREAKOUT_MAX_LIQUIDITY_USD
            and mcap is not None
            and _PUMP_EARLY_BREAKOUT_MIN_MARKET_CAP_USD <= mcap <= _PUMP_EARLY_BREAKOUT_MAX_MARKET_CAP_USD
            and price_pct_5m is not None
            and _PUMP_EARLY_BREAKOUT_MIN_PRICE_PCT_5M <= price_pct_5m <= _PUMP_EARLY_BREAKOUT_MAX_PRICE_PCT_5M
            and txns_5m >= _PUMP_EARLY_BREAKOUT_MIN_TXNS_5M
            and volume_24h is not None
            and volume_24h >= _PUMP_EARLY_BREAKOUT_MIN_VOLUME_USD_24H
            and score_total >= _PUMP_EARLY_BREAKOUT_MIN_SCORE_TOTAL
        )

    def _historical_trade_matches_current_profit_gate(
        *,
        regime: object,
        buy_dex_id: object,
        buy_liquidity_is_proxy: object,
        buy_liquidity_usd: object,
        buy_market_cap_usd: object,
        buy_volume_24h_usd: object,
        buy_price_pct_5m: object,
        buy_txns_last_5m: object,
        entry_score_total: object,
        gate_profile: object,
    ) -> bool:
        if str(regime or "").strip().lower() != "pump_early":
            return True
        if not _PUMP_EARLY_PROFIT_HEALTH_REBASE_CURRENT_GATE:
            return True

        dex_id = str(buy_dex_id or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")
        if dex_id not in _PUMP_EARLY_PROFIT_DEX_ALLOWLIST:
            return False

        liquidity = _hist_float(buy_liquidity_usd)
        mcap = _hist_float(buy_market_cap_usd)
        volume_24h = _hist_float(buy_volume_24h_usd)
        price_pct_5m = _hist_float(buy_price_pct_5m)
        txns_5m = int(_hist_float(buy_txns_last_5m) or 0)
        score_total = int(_hist_float(entry_score_total) or 0)
        liq_proxy = bool(buy_liquidity_is_proxy)

        if liquidity is None or liquidity < _PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD:
            return False
        if _PUMP_EARLY_PROFIT_REQUIRE_REAL_LIQUIDITY and (liq_proxy or abs(float(liquidity) - 1_500.0) <= 1.0):
            return False
        if mcap is None or mcap <= 0:
            return False
        if score_total < _PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL:
            return False
        breakout_match = _historical_breakout_matches(
            buy_dex_id=buy_dex_id,
            buy_liquidity_is_proxy=buy_liquidity_is_proxy,
            buy_liquidity_usd=buy_liquidity_usd,
            buy_market_cap_usd=buy_market_cap_usd,
            buy_volume_24h_usd=buy_volume_24h_usd,
            buy_price_pct_5m=buy_price_pct_5m,
            buy_txns_last_5m=buy_txns_last_5m,
            entry_score_total=entry_score_total,
        )
        if (
            _PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD > 0
            and _PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD > 0
            and _PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD <= mcap <= _PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD
        ):
            return breakout_match
        if _price5m_blocked_bucket(price_pct_5m):
            return breakout_match

        token = {
            "dex_id": dex_id,
            "liquidity_usd": liquidity,
            "market_cap_usd": mcap,
            "volume_24h_usd": volume_24h,
            "price_pct_5m": price_pct_5m,
            "txns_last_5m": txns_5m,
            "score_total": score_total,
            "liquidity_usd_is_proxy": int(liq_proxy),
        }
        historical_meteor = bool(
            _PUMP_EARLY_METEOR_PRIME_ENABLED
            and str(gate_profile or "").strip() == "pumpswap_meteor_prime"
        )
        return not bool(_profit_shape_guard_failures(token, meteor_prime=historical_meteor)) or breakout_match

    def _historical_profit_gate_profile(
        *,
        buy_liquidity_usd: object,
        buy_market_cap_usd: object,
        buy_volume_24h_usd: object,
        buy_price_pct_5m: object,
        buy_txns_last_5m: object,
        buy_liquidity_is_proxy: object,
        buy_dex_id: object,
        entry_score_total: object,
    ) -> str:
        if _historical_breakout_matches(
            buy_dex_id=buy_dex_id,
            buy_liquidity_is_proxy=buy_liquidity_is_proxy,
            buy_liquidity_usd=buy_liquidity_usd,
            buy_market_cap_usd=buy_market_cap_usd,
            buy_volume_24h_usd=buy_volume_24h_usd,
            buy_price_pct_5m=buy_price_pct_5m,
            buy_txns_last_5m=buy_txns_last_5m,
            entry_score_total=entry_score_total,
        ):
            return "pumpswap_breakout_probe"
        liquidity = _hist_float(buy_liquidity_usd) or 0.0
        mcap = _hist_float(buy_market_cap_usd) or 0.0
        if 0.0 < mcap < 25_000.0 and _PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD <= liquidity <= 25_000.0:
            return "pumpswap_profit_prime"
        return "pumpswap_profit_broad"

    stmt = (
        select(
            Position.entry_regime,
            Position.total_pnl_pct,
            Position.closed_at,
            Position.exit_reason,
            Position.size_bucket,
            Position.entry_lane,
            Position.buy_dex_id,
            Position.buy_liquidity_is_proxy,
            Position.mcap_bucket,
            Position.price5m_bucket,
            Position.gate_profile,
            Position.buy_liquidity_usd,
            Position.buy_market_cap_usd,
            Position.buy_volume_24h_usd,
            Position.buy_price_pct_5m,
            Position.buy_txns_last_5m,
            Position.entry_score_total,
        )
        .where(Position.closed.is_(True), Position.total_pnl_pct.is_not(None))
        .order_by(Position.closed_at.asc())
        .limit(200)
    )
    rows = (await ses.execute(stmt)).all()
    history: list[tuple[object, ...]] = []
    skipped_current_gate = 0
    rebased_current_gate = 0
    for (
        regime,
        total_pnl_pct,
        closed_at,
        exit_reason,
        size_bucket,
        entry_lane,
        buy_dex_id,
        buy_liquidity_is_proxy,
        mcap_bucket,
        price5m_bucket,
        gate_profile,
        buy_liquidity_usd,
        buy_market_cap_usd,
        buy_volume_24h_usd,
        buy_price_pct_5m,
        buy_txns_last_5m,
        entry_score_total,
    ) in rows:
        matches_current_gate = _historical_trade_matches_current_profit_gate(
            regime=regime,
            buy_dex_id=buy_dex_id,
            buy_liquidity_is_proxy=buy_liquidity_is_proxy,
            buy_liquidity_usd=buy_liquidity_usd,
            buy_market_cap_usd=buy_market_cap_usd,
            buy_volume_24h_usd=buy_volume_24h_usd,
            buy_price_pct_5m=buy_price_pct_5m,
            buy_txns_last_5m=buy_txns_last_5m,
            entry_score_total=entry_score_total,
            gate_profile=gate_profile,
        )
        if not matches_current_gate:
            skipped_current_gate += 1
            continue
        rebased_lane = str(entry_lane) if entry_lane else None
        rebased_profile = str(gate_profile) if gate_profile else None
        if _PUMP_EARLY_PROFIT_HEALTH_REBASE_CURRENT_GATE and str(regime or "").strip().lower() == "pump_early":
            rebased_current_gate += 1
            rebased_profile = _historical_profit_gate_profile(
                buy_dex_id=buy_dex_id,
                buy_liquidity_is_proxy=buy_liquidity_is_proxy,
                buy_liquidity_usd=buy_liquidity_usd,
                buy_market_cap_usd=buy_market_cap_usd,
                buy_volume_24h_usd=buy_volume_24h_usd,
                buy_price_pct_5m=buy_price_pct_5m,
                buy_txns_last_5m=buy_txns_last_5m,
                entry_score_total=entry_score_total,
            )
            rebased_lane = (
                "pump_early_pumpswap_breakout_probe"
                if rebased_profile == "pumpswap_breakout_probe"
                else "pump_early_pumpswap_profit"
            )
        history.append(
            (
                str(regime or "dex_mature"),
                float(total_pnl_pct),
                closed_at,
                str(exit_reason) if exit_reason else None,
                str(size_bucket) if size_bucket else None,
                rebased_lane,
                str(buy_dex_id) if buy_dex_id else None,
                bool(buy_liquidity_is_proxy),
                str(mcap_bucket) if mcap_bucket else None,
                str(price5m_bucket) if price5m_bucket else None,
                rebased_profile,
            )
        )
    if skipped_current_gate or rebased_current_gate:
        log.info(
            "strategy_runtime bootstrap rebased current profit gate: skipped=%d rebased=%d kept=%d",
            skipped_current_gate,
            rebased_current_gate,
            len(history),
        )
    strategy_runtime.bootstrap_closed_trades(history)


async def _repair_position_entry_notionals(ses: SessionLocal) -> int:
    sol_usd = await get_sol_usd()
    if sol_usd is None or sol_usd <= 0:
        return 0

    stmt = select(Position).where(Position.buy_amount_sol.is_not(None))
    rows = (await ses.execute(stmt)).scalars().all()
    updated = 0
    for pos in rows:
        changed = False
        amount_sol = float(getattr(pos, "buy_amount_sol", 0.0) or 0.0)
        if amount_sol > 0.0 and float(getattr(pos, "entry_notional_usd", 0.0) or 0.0) <= 0.0:
            pos.entry_notional_usd = float(amount_sol * float(sol_usd))
            changed = True
        if bool(getattr(pos, "closed", False)):
            _seal_closed_trade_metrics(pos, getattr(pos, "close_price_usd", None))
            changed = True
        elif int(getattr(pos, "realized_qty", 0) or 0) > 0:
            changed = _refresh_position_trade_metrics(pos) or changed
        if changed:
            updated += 1

    if updated:
        try:
            await ses.commit()
            log.info("Backfill DB entry_notional_usd/PnL aplicado a %d posiciones", updated)
        except Exception:
            try:
                await ses.rollback()
            except Exception:
                pass
            return 0
    return updated


async def _ensure_position_entry_notional(pos: Position, ses: SessionLocal) -> bool:
    current = float(getattr(pos, "entry_notional_usd", 0.0) or 0.0)
    changed = False
    if current <= 0.0:
        amount_sol = float(getattr(pos, "buy_amount_sol", 0.0) or 0.0)
        if amount_sol <= 0.0:
            return False
        sol_usd = await get_sol_usd()
        if sol_usd is None or sol_usd <= 0:
            return False
        pos.entry_notional_usd = float(amount_sol * float(sol_usd))
        changed = True
    if bool(getattr(pos, "closed", False)):
        _seal_closed_trade_metrics(pos, getattr(pos, "close_price_usd", None))
        changed = True
    elif int(getattr(pos, "realized_qty", 0) or 0) > 0:
        changed = _refresh_position_trade_metrics(pos) or changed
    if not changed:
        return False
    try:
        await ses.commit()
        return True
    except Exception:
        try:
            await ses.rollback()
        except Exception:
            pass
        return False


def _log_strategy_health_snapshot(now: Optional[dt.datetime] = None) -> None:
    snapshot = strategy_runtime.describe_regime_health(now)
    parts: list[str] = []
    for regime, data in snapshot.items():
        parts.append(
            "%s=%s/%s avg=%s%% win=%s%% exec=%s%% price=%s%% severe=%s"
            % (
                regime,
                data.get("health_state"),
                data.get("effective_execution_state"),
                _fmt(data.get("avg_pnl_pct"), "{:.1f}"),
                _fmt((data.get("win_rate") or 0.0) * 100.0 if data.get("win_rate") is not None else None, "{:.0f}"),
                _fmt((data.get("exec_rate") or 0.0) * 100.0 if data.get("exec_rate") is not None else None, "{:.0f}"),
                _fmt((data.get("price_rate") or 0.0) * 100.0 if data.get("price_rate") is not None else None, "{:.0f}"),
                _fmt(data.get("severe_exit_count"), "{:.0f}"),
            )
        )
        log_regime_health_event(
            regime,
            requested_mode=data.get("requested_mode"),
            effective_execution_state=data.get("effective_execution_state"),
            health_state=data.get("health_state"),
            trade_count=int(data.get("trade_count") or 0),
            avg_pnl_pct=data.get("avg_pnl_pct"),
            short_avg_pnl_pct=data.get("short_avg_pnl_pct"),
            win_rate=data.get("win_rate"),
            exec_rate=data.get("exec_rate"),
            price_rate=data.get("price_rate"),
            consecutive_losses=int(data.get("consecutive_losses") or 0),
            cooldown_until=data.get("cooldown_until"),
            disable_reason=data.get("disable_reason"),
            last_disable_reason=data.get("last_disable_reason"),
            size_cap_multiplier=data.get("size_cap_multiplier"),
            severe_exit_count=int(data.get("severe_exit_count") or 0),
            recovery_trade_count=int(data.get("recovery_trade_count") or 0),
            recovery_avg_pnl_pct=data.get("recovery_avg_pnl_pct"),
            recovery_ready=bool(data.get("recovery_ready")),
            last_auto_demote_at=data.get("last_auto_demote_at"),
            last_auto_recover_at=data.get("last_auto_recover_at"),
        )
    if parts:
        log.info("Strategy health: %s", " | ".join(parts))


# ╭─────────────────────── Loop de entrenamiento ─────────────────────────────╮
async def retrain_loop() -> None:
    import calendar
    global _runtime_retrain_state

    retrain_frequency = str(getattr(CFG, "RETRAIN_FREQUENCY", "weekly") or "weekly").strip().lower()
    if retrain_frequency not in {"daily", "weekly"}:
        retrain_frequency = "weekly"

    weekday = calendar.day_name[CFG.RETRAIN_DAY]
    if retrain_frequency == "daily":
        log.info("Retrain-loop activo (daily %02d:00 UTC)", CFG.RETRAIN_HOUR)
    else:
        log.info("Retrain-loop activo (%s %02d:00 UTC)", weekday, CFG.RETRAIN_HOUR)

    while True:
        now = utc_now()
        in_retrain_day = True if retrain_frequency == "daily" else (now.weekday() == CFG.RETRAIN_DAY)
        if (
            in_retrain_day
            and now.hour   == CFG.RETRAIN_HOUR
            and now.minute < 15   # ← ampliamos ventana (antes: < 10)
        ):
            # Log explícito de entrada en ventana
            try:
                log.info("⏰ Ventana de retraining abierta (UTC=%s)", now.strftime("%Y-%m-%d %H:%M"))
            except Exception:
                pass

            try:
                if _retrain_lock.locked():
                    log.info("Retrain-loop omitido: retrain ya en curso")
                else:
                    await _run_retrain_once(source="retrain_loop")
            except Exception as exc:
                log.error("Retrain error: %s", exc)

            # Evitar disparos repetidos dentro de la misma hora
            await asyncio.sleep(3600)
            continue

            try:
                _runtime_retrain_state = "running"
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
                _runtime_retrain_state = "idle"
            except Exception as exc:
                _runtime_retrain_state = "failed"
                _note_runtime_error("retrain_loop", exc)
                log.error("Retrain error: %s", exc)

            # Evitar disparos repetidos dentro de la misma hora
            await asyncio.sleep(3600)
        else:
            await asyncio.sleep(300)


# ╭─────────────────────── Main loop ─────────────────────────────────────────╮
async def main_loop() -> None:
    global _runtime_started_at, _runtime_process_state
    global _last_discovery_ok_at, _last_monitor_ok_at, _runtime_reports_refresh_state
    global _wallet_sol_balance, _last_stats_print, _last_csv_export, _last_wallet_checked_at
    ses             = SessionLocal()
    last_discovery  = 0.0
    if _runtime_started_at is None:
        _runtime_started_at = utc_now()
    _runtime_process_state = "starting"

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
    policy = filters.describe_filter_policy()
    log.info(
        "⚙️  Filter policy: profile_by_discovery=%s · snapshot_quality=%s · max_missing=%s · price_sources=%s",
        str(policy["profile_by_discovery"]),
        str(policy["snapshot_quality_filter_enabled"]),
        policy["snapshot_max_missing_fields"],
        policy["snapshot_allowed_price_sources"],
    )
    sizing_policy = entry_sizing.describe_sizing_policy()
    log.info(
        "Trade amount policy: mode=%s default=%.3f SOL min=%.3f SOL multipliers_affect_amount=%s",
        str(sizing_policy.get("trade_amount_mode") or "fixed"),
        float(sizing_policy.get("default_trade_amount_sol") or 0.0),
        float(sizing_policy.get("min_buy_sol") or 0.0),
        str(sizing_policy.get("multipliers_affect_trade_amount")),
    )
    log.info(
        "⚙️  Sizing policy: dynamic=%s · pump_early_age<=%.1fm · multipliers=%s/%s/%s · regime_caps=%s",
        str(sizing_policy["dynamic_sizing_enabled"]),
        sizing_policy["pump_early_max_age_min"],
        _fmt(sizing_policy["size_multipliers"]["recovery"], "{:.2f}"),
        _fmt(sizing_policy["size_multipliers"]["standard"], "{:.2f}"),
        _fmt(sizing_policy["size_multipliers"]["standard"], "{:.2f}"),
        json.dumps(sizing_policy["regime_position_caps"], ensure_ascii=True, sort_keys=True),
    )
    if _DEX_MATURE_QUALITY_MIN_POINTS > 0:
        log.info(
            "⚙️  Dex quality gate: points>=%d · age>=%.1fm · liq>=%.0f · vol>=%.0f · mcap>=%.0f · holders>=%d · score>=%d · backoff=%ss",
            _DEX_MATURE_QUALITY_MIN_POINTS,
            _DEX_MATURE_QUALITY_MIN_AGE_MIN,
            _DEX_MATURE_QUALITY_MIN_LIQUIDITY_USD,
            _DEX_MATURE_QUALITY_MIN_VOLUME_USD_24H,
            _DEX_MATURE_QUALITY_MIN_MARKET_CAP_USD,
            _DEX_MATURE_QUALITY_MIN_HOLDERS,
            _DEX_MATURE_QUALITY_MIN_SCORE_TOTAL,
            _DEX_MATURE_QUALITY_BACKOFF_S,
        )
    if _PUMP_EARLY_QUALITY_MIN_POINTS > 0:
        log.info(
            "⚙️  Pump quality gate: points>=%d · age>=%.1fm · liq>=%.0f · vol>=%.0f · mcap>=%.0f · holders>=%d · score>=%d · impact<=%.1f · backoff=%ss",
            _PUMP_EARLY_QUALITY_MIN_POINTS,
            _PUMP_EARLY_QUALITY_MIN_AGE_MIN,
            _PUMP_EARLY_QUALITY_MIN_LIQUIDITY_USD,
            _PUMP_EARLY_QUALITY_MIN_VOLUME_USD_24H,
            _PUMP_EARLY_QUALITY_MIN_MARKET_CAP_USD,
            _PUMP_EARLY_QUALITY_MIN_HOLDERS,
            _PUMP_EARLY_QUALITY_MIN_SCORE_TOTAL,
            _PUMP_EARLY_QUALITY_MAX_PRICE_IMPACT_PCT,
            _PUMP_EARLY_QUALITY_BACKOFF_S,
        )
    log.info(
        "⚙️  Flow guards: gecko_min_queue_age=%ss · policy_reject_dedup=%ss · pump_stream_no_liq=%ss · pump_stream_reject=%ss",
        _GECKO_MIN_QUEUE_AGE_S,
        int(_POLICY_REJECT_DEDUP_TTL_S),
        _PUMPFUN_STREAM_COOLDOWN_NO_LIQ_S,
        _PUMPFUN_STREAM_COOLDOWN_REJECT_S,
    )
    if (
        _PUMP_EARLY_LIVE_HARD_MIN_AGE_MIN > 0
        or _PUMP_EARLY_LIVE_HARD_MIN_LIQUIDITY_USD > 0
        or _PUMP_EARLY_LIVE_HARD_MIN_SCORE_TOTAL > 0
        or _PUMP_EARLY_LIVE_HARD_MIN_VOLUME_USD_24H > 0
    ):
        log.info(
            "⚙️  Pump live gate: age>=%.1fm · liq>=%.0f · score>=%.0f · mcap>=%.0f · missing<=%d",
            _PUMP_EARLY_LIVE_MIN_AGE_EFFECTIVE,
            _PUMP_EARLY_LIVE_MIN_LIQUIDITY_EFFECTIVE,
            _PUMP_EARLY_LIVE_MIN_SCORE_EFFECTIVE,
            _PUMP_EARLY_LIVE_MIN_MARKET_CAP_EFFECTIVE,
            _PUMP_EARLY_LIVE_MAX_SNAPSHOT_MISSING_FIELDS,
        )
    if _PUMP_EARLY_SNIPER_ENABLED:
        log.info(
            "Pump sniper lane: mode=%s core(age=%.1f-%.1fm liq>=%.0f mcap=%.0f-%.0f score>=%d rank>=%.1f txns5m>=%d price5m=[%.1f,%.1f] impact<=%.1f) micro(liq>=%.0f vol>=%.0f score>=%d rank>=%.1f txns5m>=%d price5m>=%.1f impact<=%.1f)",
            _PUMP_EARLY_SNIPER_MODE,
            _PUMP_EARLY_SNIPER_MIN_AGE_MIN,
            _PUMP_EARLY_SNIPER_MAX_AGE_MIN,
            _PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD,
            _PUMP_EARLY_SNIPER_MIN_MARKET_CAP_USD,
            _PUMP_EARLY_SNIPER_MAX_MARKET_CAP_USD,
            _PUMP_EARLY_SNIPER_MIN_SCORE_TOTAL,
            _PUMP_EARLY_SNIPER_MIN_RANK_SCORE,
            _PUMP_EARLY_SNIPER_MIN_TXNS_5M,
            _PUMP_EARLY_SNIPER_MIN_PRICE_PCT_5M,
            _PUMP_EARLY_SNIPER_MAX_PRICE_PCT_5M,
            _PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT,
            _PUMP_EARLY_SNIPER_MICRO_MIN_LIQUIDITY_USD,
            _PUMP_EARLY_SNIPER_MICRO_MIN_VOLUME_USD_24H,
            _PUMP_EARLY_SNIPER_MICRO_MIN_SCORE_TOTAL,
            _PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE,
            _PUMP_EARLY_SNIPER_MICRO_MIN_TXNS_5M,
            _PUMP_EARLY_SNIPER_MICRO_MIN_PRICE_PCT_5M,
            _PUMP_EARLY_SNIPER_MICRO_MAX_PRICE_IMPACT_PCT,
        )
    if _PAPER_COLD_START_ENABLED:
        log.info(
            "⚙️  Paper cold-start gate: dry_run_only=true · closed<%d · age>=%.1fm · liq>=%.0f · score>=%.0f · mcap>=%.0f · missing<=%d · rank>=%.1f · price5m=[%.1f,%.1f] · shadow_probe=%s@%.2fx",
            _PAPER_COLD_START_MAX_CLOSED_TRADES,
            _PAPER_COLD_START_MIN_AGE_MIN,
            _PAPER_COLD_START_MIN_LIQUIDITY_USD,
            _PAPER_COLD_START_MIN_SCORE_TOTAL,
            _PAPER_COLD_START_MIN_MARKET_CAP_USD,
            _PAPER_COLD_START_MAX_SNAPSHOT_MISSING_FIELDS,
            _PAPER_COLD_START_MIN_RANK_SCORE,
            _PAPER_COLD_START_MIN_PRICE_PCT_5M,
            _PAPER_COLD_START_MAX_PRICE_PCT_5M,
            str(_PAPER_COLD_START_SHADOW_PROBE_ENABLED),
            _PAPER_COLD_START_SHADOW_PROBE_SIZE_MULTIPLIER,
        )
    if _PAPER_AGGRESSIVE_TRADING_ENABLED:
        log.info(
            "Paper aggressive mode: dry_run_only=true Â· regimes=live Â· confirmations=%s Â· age>=%.2fm Â· liq>=%.0f Â· mcap=%.0f-%.0f Â· score>=%d Â· rank>=%.1f Â· txns5m>=%d Â· missing<=%d Â· impact<=%.1f",
            getattr(CFG, "PAPER_AGGRESSIVE_CONFIRM_SNAPSHOTS", 1),
            _PAPER_AGGRESSIVE_MIN_AGE_MIN,
            _PAPER_AGGRESSIVE_MIN_LIQUIDITY_USD,
            _PAPER_AGGRESSIVE_MIN_MARKET_CAP_USD,
            _PAPER_AGGRESSIVE_MAX_MARKET_CAP_USD,
            _PAPER_AGGRESSIVE_MIN_SCORE_TOTAL,
            _PAPER_AGGRESSIVE_MIN_RANK_SCORE,
            _PAPER_AGGRESSIVE_MIN_TXNS_5M,
            _PAPER_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS,
            _PAPER_AGGRESSIVE_MAX_PRICE_IMPACT_PCT,
        )
        if _PUMP_EARLY_AGGRESSIVE_RESEARCH_GUARD_ENABLED:
            log.info(
                "Paper aggressive guard: dex_allowlist=%s · block_price5m=%s · high_mcap>=%.0f unless txns5m>=%d and price5m=25-50 · block_proxy=%s",
                ",".join(sorted(_PUMP_EARLY_AGGRESSIVE_RESEARCH_DEX_ALLOWLIST)) or "*",
                getattr(CFG, "PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PRICE5M_RANGES", "25:999"),
                _PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_HIGH_MCAP_USD,
                _PUMP_EARLY_AGGRESSIVE_RESEARCH_HIGH_MCAP_ALLOW_MIN_TXNS_5M,
                str(_PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PROXY),
            )
    if _LIVE_AGGRESSIVE_TRADING_ENABLED:
        log.info(
            "Live aggressive mode: dry_run_only=false Â· regimes=live Â· confirmations=%s Â· age>=%.2fm Â· liq>=%.0f Â· mcap=%.0f-%.0f Â· score>=%d Â· rank>=%.1f Â· txns5m>=%d Â· missing<=%d Â· impact<=%.1f Â· health_continue=%s",
            getattr(CFG, "LIVE_AGGRESSIVE_CONFIRM_SNAPSHOTS", 1),
            _LIVE_AGGRESSIVE_MIN_AGE_MIN,
            _LIVE_AGGRESSIVE_MIN_LIQUIDITY_USD,
            _LIVE_AGGRESSIVE_MIN_MARKET_CAP_USD,
            _LIVE_AGGRESSIVE_MAX_MARKET_CAP_USD,
            _LIVE_AGGRESSIVE_MIN_SCORE_TOTAL,
            _LIVE_AGGRESSIVE_MIN_RANK_SCORE,
            _LIVE_AGGRESSIVE_MIN_TXNS_5M,
            _LIVE_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS,
            _LIVE_AGGRESSIVE_MAX_PRICE_IMPACT_PCT,
            str(bool(getattr(CFG, "LIVE_AGGRESSIVE_CONTINUE_ON_HEALTH", True))),
        )
    strategy_policy = strategy_runtime.describe_strategy_policy()
    log.info(
        "⚙️  Strategy policy: pump=%s/%sx@%sm · dex=%s/%sx@%sm · revival=%s/%sx@%sm",
        strategy_policy["pump_early"]["mode"],
        strategy_policy["pump_early"]["confirmations"],
        _fmt(strategy_policy["pump_early"]["min_age_min"], "{:.1f}"),
        strategy_policy["dex_mature"]["mode"],
        strategy_policy["dex_mature"]["confirmations"],
        _fmt(strategy_policy["dex_mature"]["min_age_min"], "{:.1f}"),
        strategy_policy["revival"]["mode"],
        strategy_policy["revival"]["confirmations"],
        _fmt(strategy_policy["revival"]["min_age_min"], "{:.1f}"),
    )
    exit_cfg = exit_policy.describe_exit_policy()
    # Banner exit extras
    log.info(
        "⚙️  Exit policy: by_regime=%s · tp_partial=%s trigger=%.1f%% frac=%.2f · post_partial_stop=%.1f%% trail=%.1f%% · no_pump=(%sm,%s%%) · time_stop=(%sm,peak<%s%%,pnl<=%s%%)",
        str(exit_cfg["exit_profile_by_regime"]),
        str(exit_cfg["tp_partial_enabled"]),
        float(exit_cfg["tp_partial_trigger_pct"]),
        float(exit_cfg["tp_partial_fraction"]),
        float(exit_cfg["post_partial_stop_pct"]),
        float(exit_cfg["post_partial_trailing_pct"]),
        _fmt(exit_cfg["no_pump_window_min"], "{:.0f}"),
        _fmt(exit_cfg["no_pump_min_pnl_pct"], "{:.0f}"),
        _fmt(exit_cfg["time_stop_min"], "{:.0f}"),
        _fmt(exit_cfg["time_stop_min_peak_pct"], "{:.0f}"),
        _fmt(exit_cfg["time_stop_max_pnl_pct"], "{:.0f}"),
    )
    ml_gate = _ml_gate_state()
    log.info(
        "⚙️  ML policy: mode=%s · enforce=%s · activation_ready=%s · model_loaded=%s · threshold_metric=%s · rows=%s",
        ml_gate["mode"],
        str(ml_gate["enforce"]),
        str(ml_gate["activation_ready"]),
        str(ml_gate["model_loaded"]),
        str(ml_gate["threshold_metric"]),
        str(ml_gate["rows"]),
    )
    log.info(
        "⚙️  Research lane: enabled=%s · shadow=%s · cap=%s/%s · min_rank=%.1f · scorecard=%sm",
        str(bool(getattr(CFG, "RESEARCH_LANE_ENABLED", True))),
        str(bool(getattr(CFG, "RESEARCH_SHADOW_ENABLED", True))),
        str(int(getattr(CFG, "RESEARCH_SHADOW_MAX_OPEN", 8) or 8)),
        str(int(getattr(CFG, "RESEARCH_SHADOW_MAX_OPEN_PER_REGIME", 4) or 4)),
        float(getattr(CFG, "RESEARCH_SHADOW_MIN_RANK_SCORE", 55.0) or 55.0),
        int(getattr(CFG, "RESEARCH_SCORECARD_INTERVAL_MIN", 60) or 60),
    )

    try:
        _wallet_sol_balance = await get_sol_balance()
        _last_wallet_checked_at = utc_now()
        log.info("Balance inicial: %.3f SOL", _wallet_sol_balance)
    except Exception as exc:
        _wallet_sol_balance = 0.0
        _note_runtime_error("initial_balance", exc)
        log.warning("Balance inicial no disponible: %s", exc)
    if DRY_RUN and hasattr(buyer, "backfill_entry_notionals"):
        try:
            repaired = await buyer.backfill_entry_notionals()
            if repaired:
                log.info("Backfill paper_portfolio entry_notional_usd aplicado a %d posiciones", repaired)
        except Exception as exc:
            log.debug("paper_portfolio backfill → %s", exc)
    await _repair_position_entry_notionals(ses)
    await _bootstrap_strategy_runtime(ses)
    _log_strategy_health_snapshot()
    try:
        await _refresh_reports_once(source="research_scorecard_init", force=True, include=("research",))
    except Exception as exc:
        log.debug("research scorecard init → %s", exc)
    _runtime_process_state = "running"

    while True:
        now_mono = time.monotonic()
        await _refresh_balance(now_mono)

        # 1) Descubrimiento DexScreener
        if now_mono - last_discovery >= DISCOVERY_INTERVAL:
            if _runtime_discovery_paused:
                last_discovery = now_mono
            else:
                try:
                    for addr in await fetch_candidate_pairs():
                        _queue_add_if_new(addr)
                    _last_discovery_ok_at = utc_now()
                except Exception as exc:
                    _note_runtime_error("fetch_candidate_pairs", exc)
                    log.error("fetch_candidate_pairs → %s", exc)
                last_discovery = now_mono

        # 2) Stream Pump Fun
        if not _runtime_discovery_paused:
            try:
                for tok in await pumpfun.get_latest_pumpfun():
                    if bool(getattr(CFG, "HOT_QUEUE_ENABLED", True)):
                        GLOBAL_HOT_QUEUE.add(tok, source=str(tok.get("source") or tok.get("discovered_via") or "pumpfun"))
                    else:
                        await _evaluate_and_buy_guarded(tok, ses, source="pumpfun")
            except Exception as exc:
                _note_runtime_error("pumpfun_stream", exc)
                log.error("PumpFun stream → %s", exc)

        # 3) Validación cola
        if bool(getattr(CFG, "HOT_QUEUE_ENABLED", True)):
            try:
                for tok in GLOBAL_HOT_QUEUE.pop_batch(int(getattr(CFG, "HOT_QUEUE_BATCH_SIZE", 12) or 12)):
                    await _evaluate_and_buy_guarded(tok, ses, source="hot_queue")
            except Exception as exc:
                _note_runtime_error("hot_queue", exc)
                log.error("Hot queue -> %s", exc)

        for addr in obtener_pares()[:VALIDATION_BATCH_SIZE]:
            try:
                meta    = lista_pares.meta(addr) or {}
                queue_age_s = max(0.0, time.time() - float(meta.get("first_seen", time.time()) or time.time()))
                attempts = int(meta.get("attempts", 0) or 0)
                use_gt  = attempts >= _GECKO_MIN_QUEUE_ATTEMPTS and queue_age_s >= _GECKO_MIN_QUEUE_AGE_S
                tok     = await price_service.get_price(addr, use_gt=use_gt, allow_partial=True)
                if tok is None and not use_gt and attempts >= _GECKO_MIN_QUEUE_ATTEMPTS and queue_age_s >= _GECKO_MIN_QUEUE_AGE_S:
                    # Un primer fallback con Gecko reduce requeues "dex_nil"
                    # cuando Jupiter/Birdeye/DexScreener no completan liquidez.
                    tok = await price_service.get_price(addr, use_gt=True, allow_partial=True)
                if tok:
                    await _evaluate_and_buy_guarded(tok, ses, source="queue")
                else:
                    _requeue_with_stats(addr, reason="dex_nil")
            except Exception as exc:
                log.error("get_price %s → %s", addr[:6], exc)

        # 4) Posiciones abiertas
            try:
                await _check_positions(ses)
                _last_monitor_ok_at = utc_now()
            except Exception as exc:
                _note_runtime_error("check_positions", exc)
                log.error("Check positions → %s", exc)

        # 4.5) Shadows (modo real o estrategia shadow en paper/live)
        if _shadow_positions or (not DRY_RUN and REAL_SHADOW_SIM):
            try:
                await _tick_shadows()
            except Exception as exc:
                _note_runtime_error("tick_shadows", exc)
                log.debug("tick_shadows → %s", exc)

        # 5) Métricas embudo + estado cola
        if (now_mono := time.monotonic()) - _last_stats_print >= 60:
            log_funnel(_stats)
            pend, req, cool = queue_stats()
            log.info(
                "Queue %d pending (%d requeued, %d cooldown) requeues=%d succ=%d",
                pend, req, cool, _stats["requeues"], _stats["requeue_success"],
            )
            _log_strategy_health_snapshot()
            if _stats["raw_discovered"] and (
                _stats["incomplete"] / _stats["raw_discovered"] > 0.5
            ):
                log.warning(
                    "⚠️  Ratio incomplete alto: %.1f%%",
                    _stats["incomplete"] / _stats["raw_discovered"] * 100,
                )
            try:
                if _reports_refresh_lock.locked():
                    log.debug("research scorecard refresh skipped: refresh already running")
                else:
                    await _refresh_reports_once(source="research_scorecard_refresh", force=False, include=("research",))
            except Exception as exc:
                log.debug("research scorecard refresh → %s", exc)
            _last_stats_print = now_mono

        # 6) Export CSV cada hora
        if now_mono - _last_csv_export >= 3600:
            store_export_csv()
            _last_csv_export = now_mono

        await asyncio.sleep(SLEEP_SECONDS)


# ╭─────────────────────── Entrypoint ───────────────────────────────────────╮
async def _runner() -> None:
    global _runtime_process_state
    await async_init_db()
    _runtime_process_state = "starting"
    try:
        tasks = [
            main_loop(),
            _periodic_labeler(),
            control_command_loop(),
            runtime_state_loop(),
        ]
        if bool(getattr(CFG, "ML_RETRAIN_IN_MAIN_LOOP", False)):
            tasks.append(retrain_loop())
        else:
            log.info("Retrain-loop omitido: ML_RETRAIN_IN_MAIN_LOOP=false")
        await asyncio.gather(*tasks)
    except Exception as exc:
        _runtime_process_state = "stopped"
        _note_runtime_error("runner", exc)
        try:
            await _publish_runtime_state_once()
        except Exception as publish_exc:
            log.error("runtime state final publish → %s", publish_exc)
        raise

if __name__ == "__main__":
    try:
        _acquire_process_lock()
    except SingleInstanceLockError as exc:
        print(f"run_bot singleton guard: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        log.info("⏹️  Bot detenido por usuario")
    finally:
        _release_process_lock()
