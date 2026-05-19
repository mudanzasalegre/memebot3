# config/config.py – MemeBot 3
"""
Configuración central (CFG) + exports “legacy” para compatibilidad.

Añadidos 2025-09-15
──────────────────
• (UNIFICADO) AI_THRESHOLD ← lee también AI_TH para compatibilidad; default 0.65
• BUY_SOFT_SCORE_MIN       (puntuación mínima blanda, ej. 40)
• DEX_WHITELIST            (CSV de DEX permitidos, ej. "raydium,orca,meteora")
• REQUIRE_POOL_INITIALIZED (bool; exigir pool inicializado)
• BUY_RATE_LIMIT_N         (compras máximas por ventana)
• BUY_RATE_LIMIT_WINDOW_S  (tamaño de ventana para rate-limit de compras)
• MIN_AGE_MIN              (sube default a 3.0 min; también desde .env)

Añadidos 2025-08-02
──────────────────
• GECKO_API_URL          (endpoint base GeckoTerminal)
• INCOMPLETE_RETRIES     (reintentos rápidos 0-delay en run_bot)
• MAX_RETRIES            (re-queues permitidos en utils.lista_pares)

Añadidos 2025-08-15
──────────────────
• USE_JUPITER_PRICE      (activar Jupiter Price v3 Lite)
• JUPITER_PRICE_URL      (endpoint base)
• JUPITER_RPM            (rate-limit sencillo en fetcher/jupiter_price)
• JUPITER_TTL_NIL_SHORT  (TTL caché negativa corto)
• JUPITER_TTL_NIL_MAX    (TTL caché negativa máximo)
• JUPITER_TTL_OK         (TTL caché positiva, opcional)

Añadidos 2025-08-23
──────────────────
• TRADING_WINDOWS        (legacy)
• TRADING_STRICT         (legacy)
• REQUIRE_JUPITER_FOR_BUY (exigir Jupiter “solo precio” en la entrada)
• EARLY_DROP_KILL_PCT/EARLY_DROP_WINDOW_MIN
• LIQ_CRUSH_DROP_PCT/LIQ_CRUSH_WINDOW_MIN
• AI_THRESHOLD_FILE

Añadidos 2025-08-28
──────────────────
• TRADING_HOURS, TRADING_HOURS_EXTRA, USE_EXTRA_HOURS (control horario vía .env)
• LOCAL_TZ (zona horaria para filtros/telemetría; por defecto Europe/Madrid)
• DEXS_TXNS_5M_MIN (umbral mínimo de swaps 5m si la señal viene de DexScreener)
• USE_JUPITER_IMPACT, IMPACT_PROBE_SOL, IMPACT_MAX_PCT (sonda de impacto/slippage)
• MAX_HARD_HOLD_H (límite duro opcional cuando se extiende por trailing)

Añadidos 2025-09-13
──────────────────
• FORCE_JUP_IN_MONITOR    (forzar uso de Jupiter en el monitor aunque la compra no fuese JUP)
• REAL_SHADOW_SIM         (activar shadow-simulation en modo REAL para dataset)
• TRAIN_FORWARD_HOLDOUT_DAYS / TRAIN_FORWARD_HOLDOUT_PCT (hold-out hacia delante para validación)
• TRAINING_WINDOW_DAYS    (ventana móvil de entrenamiento)
• MIN_THRESHOLD_CHANGE    (suavizado del umbral recomendado antes de sobrescribir)
• BLOCK_HOURS             (horas locales bloqueadas, ej. "3,12,17-19")
"""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass
from typing import Callable, TypeVar, Tuple

from zoneinfo import ZoneInfo
from trade_pnl import resolve_take_profit_and_win_pct

# python-dotenv es opcional pero normalmente está instalado en el proyecto
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore

T = TypeVar("T", int, float)
_num_re = re.compile(r"-?\d+(?:\.\d+)?")

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


# ───────────────────────── helpers ──────────────────────────
def _bool_env(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    s = str(raw).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return default


def _num_env(key: str, cast: Callable[[str], T], default: T) -> T:
    """Lee key numérica del .env con casting seguro y fallback."""
    raw = os.getenv(key, str(default))
    m = _num_re.search(raw or "")
    try:
        return cast(m.group()) if m else default
    except (ValueError, TypeError):
        return default


def _num_env_multi(names: list[str], cast: Callable[[str], T], default: T) -> T:
    """
    Lee la primera variable existente de `names` (en orden de prioridad),
    con casting seguro. Útil para migraciones/back-compat (AI_THRESHOLD vs AI_TH).
    """
    for key in names:
        if os.getenv(key) is not None:
            return _num_env(key, cast, default)
    return default


def _opt_num_env(key: str, cast: Callable[[str], T]) -> T | None:
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return None
    m = _num_re.search(str(raw))
    try:
        return cast(m.group()) if m else None
    except (ValueError, TypeError):
        return None


def _opt_bool_env(key: str) -> bool | None:
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return None
    s = str(raw).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def _csv_tuple(raw: str, *, lower: bool = True, strip: bool = True) -> Tuple[str, ...]:
    """
    Convierte un CSV en tupla normalizada (sin entradas vacías).
    lower=True → pasa a minúsculas; strip=True → quita espacios.
    """
    if not raw:
        return tuple()

    items: list[str] = []
    for part in str(raw).split(","):
        s = part.strip() if strip else part
        if not s:
            continue
        items.append(s.lower() if lower else s)

    # dedup preservando orden
    seen: set[str] = set()
    out: list[str] = []
    for s in items:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return tuple(out)


# Legacy: parser de ventanas compactas (conservar para compatibilidad)
def _parse_windows(raw: str) -> tuple[tuple[int, int], ...]:
    """
    Parseo robusto de TRADING_WINDOWS (legacy).
    Soporta: "13-16", "7,11,13-16,18,22", espacios, duplicados.
    Devuelve tuplas (inicio, fin) inclusivas, 0–23.
    """
    if not raw:
        return tuple()

    out: list[tuple[int, int]] = []
    for chunk in str(raw).split(","):
        c = chunk.strip()
        if not c:
            continue
        if "-" in c:
            a, b = [x.strip() for x in c.split("-", 1)]
            try:
                ia, ib = int(a), int(b)
            except ValueError:
                continue
            ia = max(0, min(23, ia))
            ib = max(0, min(23, ib))
            if ia > ib:
                ia, ib = ib, ia
            out.append((ia, ib))
        else:
            try:
                h = int(c)
            except ValueError:
                continue
            h = max(0, min(23, h))
            out.append((h, h))

    if not out:
        return tuple()

    out.sort()
    merged: list[tuple[int, int]] = []
    cur_s, cur_e = out[0]
    for s, e in out[1:]:
        if s <= cur_e + 1:
            cur_e = max(cur_e, e)
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))
    return tuple(merged)


# ───────────────────────── .env loading ─────────────────────
PKG_DIR = pathlib.Path(__file__).resolve().parent


def _find_project_root(start: pathlib.Path) -> pathlib.Path:
    """Sube directorios hasta encontrar .env o /data."""
    for p in [start] + list(start.parents):
        if (p / ".env").exists() or (p / "data").is_dir():
            return p
    return start


PROJECT_ROOT = _find_project_root(PKG_DIR)

if load_dotenv is not None:
    # override=True para que el .env del proyecto mande frente a variables heredadas
    load_dotenv(PROJECT_ROOT / ".env", override=True)


def _load_config_profile() -> pathlib.Path | None:
    profile = str(os.getenv("CONFIG_PROFILE") or "").strip()
    if not profile:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", profile):
        raise RuntimeError(f"CONFIG_PROFILE inválido: {profile!r}")
    profile_path = PROJECT_ROOT / "config" / "profiles" / f"{profile}.env"
    if not profile_path.exists():
        raise RuntimeError(f"CONFIG_PROFILE no encontrado: {profile_path}")
    if load_dotenv is None:
        raise RuntimeError("CONFIG_PROFILE requiere python-dotenv para cargar perfiles .env")
    load_dotenv(profile_path, override=True)
    os.environ["CONFIG_PROFILE"] = profile
    os.environ["CONFIG_PROFILE_PATH"] = str(profile_path)
    return profile_path


CONFIG_PROFILE_PATH = _load_config_profile()


_TAKE_PROFIT_PCT_VALUE, _WIN_PCT_VALUE = resolve_take_profit_and_win_pct(
    take_profit_pct_raw=os.getenv("TAKE_PROFIT_PCT"),
    win_pct_raw=os.getenv("WIN_PCT"),
    default_take_profit_pct=35.0,
)
_ML_POSITIVE_PNL_PCT_VALUE = _num_env("ML_POSITIVE_PNL_PCT", float, _WIN_PCT_VALUE * 100.0)


# ───────────────────────── Config dataclass ─────────────────
@dataclass(frozen=True)
class _Config:
    # ------- modo ---------------------------------------------------
    CONFIG_PROFILE: str = str(os.getenv("CONFIG_PROFILE") or "").strip()
    DRY_RUN: bool = _bool_env("DRY_RUN", False)
    STRATEGY_OPTIMIZATION_LOCK: bool = _bool_env("STRATEGY_OPTIMIZATION_LOCK", True)
    AUTO_PROMOTE_LIVE: bool = _bool_env("AUTO_PROMOTE_LIVE", False)
    MODEL_AUTO_PROMOTE: bool = _bool_env("MODEL_AUTO_PROMOTE", False)
    LLM_TRADING_ENABLED: bool = _bool_env("LLM_TRADING_ENABLED", False)
    ALLOW_LIVE_POLICY_ENFORCE: bool = _bool_env("ALLOW_LIVE_POLICY_ENFORCE", False)
    REQUIRE_ENTRY_LANE_FOR_BUY: bool = _bool_env("REQUIRE_ENTRY_LANE_FOR_BUY", True)
    ALLOW_UNTAGGED_STANDARD_BUY: bool = _bool_env("ALLOW_UNTAGGED_STANDARD_BUY", False)
    DEX_MATURE_STANDARD_BUY_ENABLED: bool = _bool_env("DEX_MATURE_STANDARD_BUY_ENABLED", False)
    PUMPFUN_STANDARD_BUY_ENABLED: bool = _bool_env("PUMPFUN_STANDARD_BUY_ENABLED", False)
    UNTAGGED_BUY_SHADOW_ENABLED: bool = _bool_env("UNTAGGED_BUY_SHADOW_ENABLED", True)
    LIVE_CANARY_ENABLED: bool = _bool_env("LIVE_CANARY_ENABLED", False)
    LIVE_CANARY_MANUAL_APPROVAL: bool = _bool_env("LIVE_CANARY_MANUAL_APPROVAL", False)
    LIVE_REQUIRE_ROUTE: bool = _bool_env("LIVE_REQUIRE_ROUTE", True)
    LIVE_REQUIRE_PROVIDER_HEALTH: bool = _bool_env("LIVE_REQUIRE_PROVIDER_HEALTH", True)
    LIVE_CANARY_MAX_OPEN: int = _num_env("LIVE_CANARY_MAX_OPEN", int, 1)
    LIVE_CANARY_MAX_DAILY_BUYS: int = _num_env("LIVE_CANARY_MAX_DAILY_BUYS", int, 3)
    LIVE_CANARY_DAILY_LOSS_CAP_SOL: float = _num_env("LIVE_CANARY_DAILY_LOSS_CAP_SOL", float, 0.05)
    LIVE_CANARY_SIZE_SOL: float = _num_env("LIVE_CANARY_SIZE_SOL", float, 0.01)
    TRADE_AMOUNT_SOL: float = _num_env("TRADE_AMOUNT_SOL", float, 0.1)
    GAS_RESERVE_SOL: float = _num_env("GAS_RESERVE_SOL", float, 0.05)
    MIN_BUY_SOL: float = _num_env("MIN_BUY_SOL", float, 0.1)
    MIN_SOL_BALANCE: float = _num_env("MIN_SOL_BALANCE", float, 0.01)

    # TTLs (legacy/compat)
    DEXS_TTL_NIL: int = _num_env("DEXS_TTL_NIL", int, 300)
    DEXS_TTL_OK: int = _num_env("DEXS_TTL_OK", int, 30)

    # ------- IA / ML -----------------------------------------------
    AI_THRESHOLD: float = _num_env_multi(["AI_THRESHOLD", "AI_TH"], float, 0.65)
    BUY_SOFT_SCORE_MIN: int = _num_env("BUY_SOFT_SCORE_MIN", int, 40)
    FEATURES_DIR: pathlib.Path = pathlib.Path(os.getenv("FEATURES_DIR", PROJECT_ROOT / "data" / "features"))
    MODEL_PATH: pathlib.Path = pathlib.Path(os.getenv("MODEL_PATH", PROJECT_ROOT / "ml" / "model.pkl"))
    AI_THRESHOLD_FILE: pathlib.Path = pathlib.Path(
        os.getenv("AI_THRESHOLD_FILE", PROJECT_ROOT / "data" / "metrics" / "recommended_threshold.json")
    )

    # ⚠️ RETRAIN_FREQUENCY / RETRAIN_DAY / RETRAIN_HOUR se interpretan en **UTC**.
    #    weekday() en Python: lunes=0 … domingo=6
    RETRAIN_FREQUENCY: str = (os.getenv("RETRAIN_FREQUENCY", "weekly") or "weekly").strip().lower()
    RETRAIN_DAY: int = _num_env("RETRAIN_DAY", int, 6)
    RETRAIN_HOUR: int = _num_env("RETRAIN_HOUR", int, 4)

    # —— entrenamiento/validación ——
    TRAIN_FORWARD_HOLDOUT_DAYS: int = _num_env("TRAIN_FORWARD_HOLDOUT_DAYS", int, 3)
    TRAIN_FORWARD_HOLDOUT_PCT: float = _num_env("TRAIN_FORWARD_HOLDOUT_PCT", float, 0.0)  # 0 → ignorar
    TRAINING_WINDOW_DAYS: int = _num_env("TRAINING_WINDOW_DAYS", int, 28)
    MIN_THRESHOLD_CHANGE: float = _num_env("MIN_THRESHOLD_CHANGE", float, 0.01)  # 0.01 = 1 p.p.
    PRECISION_AT_K_PCT: float = _num_env("PRECISION_AT_K_PCT", float, 0.10)
    ML_GATE_MODE: str = (os.getenv("ML_GATE_MODE", "shadow") or "shadow").strip().lower()
    ML_SHADOW_CANDIDATE_MODEL_FALLBACK_ENABLED: bool = _bool_env(
        "ML_SHADOW_CANDIDATE_MODEL_FALLBACK_ENABLED",
        True,
    )
    ML_LIVE_PROFIT_MODE: str = (os.getenv("ML_LIVE_PROFIT_MODE", "sizing_only") or "sizing_only").strip().lower()
    ML_RESEARCH_MODE: str = (os.getenv("ML_RESEARCH_MODE", "shadow") or "shadow").strip().lower()
    ML_UNKNOWN_LANE_MODE: str = (os.getenv("ML_UNKNOWN_LANE_MODE", "shadow") or "shadow").strip().lower()
    ML_ALLOW_RESEARCH_LIVE: bool = _bool_env("ML_ALLOW_RESEARCH_LIVE", False)
    ML_ALLOW_UNKNOWN_LIVE: bool = _bool_env("ML_ALLOW_UNKNOWN_LIVE", False)
    AI_THRESHOLD_RESEARCH: float = _num_env("AI_THRESHOLD_RESEARCH", float, 0.4456)
    AI_THRESHOLD_LIVE_PROFIT: float = _num_env("AI_THRESHOLD_LIVE_PROFIT", float, 0.05)
    ML_MIN_LANE_ROWS: int = _num_env("ML_MIN_LANE_ROWS", int, 120)
    ML_MIN_LANE_POSITIVES: int = _num_env("ML_MIN_LANE_POSITIVES", int, 25)
    ML_MIN_LANE_UNIQUE_TOKENS: int = _num_env("ML_MIN_LANE_UNIQUE_TOKENS", int, 120)
    ML_MIN_LANE_HOLDOUT_ROWS: int = _num_env("ML_MIN_LANE_HOLDOUT_ROWS", int, 30)
    ML_MIN_LANE_HOLDOUT_POSITIVES: int = _num_env("ML_MIN_LANE_HOLDOUT_POSITIVES", int, 6)
    ML_MIN_JACKPOT_CAPTURE_RATE: float = _num_env("ML_MIN_JACKPOT_CAPTURE_RATE", float, 0.80)
    ML_MAX_SELECTED_PNL_DEGRADATION_PCT: float = _num_env("ML_MAX_SELECTED_PNL_DEGRADATION_PCT", float, 0.00)
    ML_RISK_MODEL_ENABLED: bool = _bool_env("ML_RISK_MODEL_ENABLED", True)
    ML_RISK_VETO_ENABLED: bool = _bool_env("ML_RISK_VETO_ENABLED", False)
    ML_RISK_VETO_THRESHOLD: float = _num_env("ML_RISK_VETO_THRESHOLD", float, 0.70)
    ML_RISK_SHADOW_ONLY: bool = _bool_env("ML_RISK_SHADOW_ONLY", True)
    ML_SEVERE_LOSS_PCT: float = _num_env("ML_SEVERE_LOSS_PCT", float, -30.0)
    ML_EV_MODEL_ENABLED: bool = _bool_env("ML_EV_MODEL_ENABLED", True)
    ML_EV_CLIP_MIN: float = _num_env("ML_EV_CLIP_MIN", float, -100.0)
    ML_EV_CLIP_MAX: float = _num_env("ML_EV_CLIP_MAX", float, 300.0)
    ML_EV_MIN_FOR_SIZE_UP: float = _num_env("ML_EV_MIN_FOR_SIZE_UP", float, 20.0)
    ML_EV_MIN_FOR_RESEARCH_BUY: float = _num_env("ML_EV_MIN_FOR_RESEARCH_BUY", float, 10.0)
    ML_RISK_PENALTY_MULT: float = _num_env("ML_RISK_PENALTY_MULT", float, 1.0)
    ML_SIZING_ENABLED: bool = _bool_env("ML_SIZING_ENABLED", True)
    ML_SIZE_MIN_MULT: float = _num_env("ML_SIZE_MIN_MULT", float, 0.25)
    ML_SIZE_MID_MULT: float = _num_env("ML_SIZE_MID_MULT", float, 0.50)
    ML_SIZE_MAX_MULT: float = _num_env("ML_SIZE_MAX_MULT", float, 1.00)
    ML_LIVE_PROFIT_PROBA_SIZE_UP: float = _num_env("ML_LIVE_PROFIT_PROBA_SIZE_UP", float, 0.30)
    ML_LIVE_PROFIT_EV_SIZE_UP: float = _num_env("ML_LIVE_PROFIT_EV_SIZE_UP", float, 50.0)
    ML_LIVE_PROFIT_EV_MIN: float = _num_env("ML_LIVE_PROFIT_EV_MIN", float, 0.0)
    ML_ALLOW_MIN_BUY_OVERRIDE: bool = _bool_env("ML_ALLOW_MIN_BUY_OVERRIDE", False)
    ML_REJECT_SHADOW_ENABLED: bool = _bool_env("ML_REJECT_SHADOW_ENABLED", True)
    ML_REJECT_SHADOW_MAX_OPEN: int = _num_env("ML_REJECT_SHADOW_MAX_OPEN", int, 10)
    ML_REJECT_SHADOW_MAX_PER_LANE: int = _num_env("ML_REJECT_SHADOW_MAX_PER_LANE", int, 5)
    ML_RETRAIN_IN_MAIN_LOOP: bool = _bool_env("ML_RETRAIN_IN_MAIN_LOOP", False)
    ML_TRAINING_DAEMON_ENABLED: bool = _bool_env("ML_TRAINING_DAEMON_ENABLED", True)
    ML_TRAINING_DAEMON_INTERVAL_S: int = _num_env("ML_TRAINING_DAEMON_INTERVAL_S", int, 900)
    ML_TRAINING_LOCK_TTL_S: int = _num_env("ML_TRAINING_LOCK_TTL_S", int, 1800)
    ML_SKIP_RETRAIN_IF_DATASET_HASH_UNCHANGED: bool = _bool_env("ML_SKIP_RETRAIN_IF_DATASET_HASH_UNCHANGED", True)
    ML_DRIFT_MONITOR_ENABLED: bool = _bool_env("ML_DRIFT_MONITOR_ENABLED", True)
    ML_DRIFT_WINDOW_TRADES: int = _num_env("ML_DRIFT_WINDOW_TRADES", int, 50)
    ML_DRIFT_MAX_MISSED_JACKPOTS: int = _num_env("ML_DRIFT_MAX_MISSED_JACKPOTS", int, 2)
    ML_DRIFT_DISABLE_ENFORCE_ON_DEGRADATION: bool = _bool_env("ML_DRIFT_DISABLE_ENFORCE_ON_DEGRADATION", True)
    ML_AUTO_PROMOTE_LANES: bool = _bool_env("ML_AUTO_PROMOTE_LANES", False)
    ML_MAX_AUTO_MODE: str = (os.getenv("ML_MAX_AUTO_MODE", "sizing_only") or "sizing_only").strip().lower()
    RESEARCH_LANE_ENABLED: bool = _bool_env("RESEARCH_LANE_ENABLED", True)
    RESEARCH_SHADOW_ENABLED: bool = _bool_env("RESEARCH_SHADOW_ENABLED", True)
    RESEARCH_DECISION_DEDUP_TTL_S: int = _num_env("RESEARCH_DECISION_DEDUP_TTL_S", int, 600)
    RESEARCH_SHADOW_MAX_OPEN: int = _num_env("RESEARCH_SHADOW_MAX_OPEN", int, 6)
    RESEARCH_SHADOW_MAX_OPEN_PER_REGIME: int = _num_env("RESEARCH_SHADOW_MAX_OPEN_PER_REGIME", int, 4)
    RESEARCH_SHADOW_MIN_RANK_SCORE: float = _num_env("RESEARCH_SHADOW_MIN_RANK_SCORE", float, 55.0)
    RESEARCH_SHADOW_MIN_AGE_MIN: float = _num_env("RESEARCH_SHADOW_MIN_AGE_MIN", float, 2.0)
    RESEARCH_SHADOW_MIN_LIQUIDITY_USD: float = _num_env("RESEARCH_SHADOW_MIN_LIQUIDITY_USD", float, 1500.0)
    RESEARCH_NEAR_MISS_SCORE_MARGIN: int = _num_env("RESEARCH_NEAR_MISS_SCORE_MARGIN", int, 8)
    RESEARCH_NEAR_MISS_PROBA_MARGIN: float = _num_env("RESEARCH_NEAR_MISS_PROBA_MARGIN", float, 0.12)
    RESEARCH_SCORECARD_INTERVAL_MIN: int = _num_env("RESEARCH_SCORECARD_INTERVAL_MIN", int, 60)
    CORE_REPORTS_AUTO_REGEN_ENABLED: bool = _bool_env("CORE_REPORTS_AUTO_REGEN_ENABLED", True)
    CORE_REPORTS_REGEN_INTERVAL_MIN: int = _num_env("CORE_REPORTS_REGEN_INTERVAL_MIN", int, 30)
    CORE_REPORTS_REGEN_ON_STARTUP: bool = _bool_env("CORE_REPORTS_REGEN_ON_STARTUP", True)
    CORE_REPORTS_REGEN_ON_CLOSES: int = _num_env("CORE_REPORTS_REGEN_ON_CLOSES", int, 25)
    RESEARCH_THRESHOLD_MIN_OUTCOMES: int = _num_env("RESEARCH_THRESHOLD_MIN_OUTCOMES", int, 20)
    RESEARCH_THRESHOLD_MIN_POSITIVES: int = _num_env("RESEARCH_THRESHOLD_MIN_POSITIVES", int, 4)
    RESEARCH_THRESHOLD_MIN_SELECTED: int = _num_env("RESEARCH_THRESHOLD_MIN_SELECTED", int, 6)
    RESEARCH_THRESHOLD_MIN_REALIZED_SELECTED: int = _num_env("RESEARCH_THRESHOLD_MIN_REALIZED_SELECTED", int, 4)
    RESEARCH_THRESHOLD_PRECISION_FLOOR: float = _num_env("RESEARCH_THRESHOLD_PRECISION_FLOOR", float, 0.55)
    ML_MIN_DATASET_ROWS: int = _num_env("ML_MIN_DATASET_ROWS", int, 250)
    ML_MIN_POSITIVES: int = _num_env("ML_MIN_POSITIVES", int, 40)
    ML_MIN_UNIQUE_TOKENS: int = _num_env("ML_MIN_UNIQUE_TOKENS", int, 200)
    ML_MIN_REALIZED_RETURN_ROWS: int = _num_env("ML_MIN_REALIZED_RETURN_ROWS", int, 50)
    ML_MIN_HOLDOUT_ROWS: int = _num_env("ML_MIN_HOLDOUT_ROWS", int, 30)
    ML_MIN_HOLDOUT_POSITIVES: int = _num_env("ML_MIN_HOLDOUT_POSITIVES", int, 10)
    ML_MIN_NON_CONSTANT_FEATURES: int = _num_env("ML_MIN_NON_CONSTANT_FEATURES", int, 12)
    ML_TUNE_OBJECTIVE: str = (
        (os.getenv("ML_TUNE_OBJECTIVE", "expected_pnl_precision_floor") or "expected_pnl_precision_floor")
        .strip()
        .lower()
    )
    ML_TUNE_PRECISION_FLOOR: float = _num_env("ML_TUNE_PRECISION_FLOOR", float, 0.60)
    ML_TUNE_MIN_SELECTED: int = _num_env("ML_TUNE_MIN_SELECTED", int, 10)
    ML_TUNE_MIN_REALIZED_SELECTED: int = _num_env("ML_TUNE_MIN_REALIZED_SELECTED", int, 5)
    ML_SELECTION_MIN_DELTA: float = _num_env("ML_SELECTION_MIN_DELTA", float, 0.25)
    ML_TRAIN_ENTRY_LANE_ALLOWLIST: str = os.getenv(
        "ML_TRAIN_ENTRY_LANE_ALLOWLIST",
        "pump_early_pumpswap_profit,pump_early_pumpswap_prime,pump_early_pumpswap_rebound_prime,pump_early_meteor_prime,pump_early_pumpswap_breakout_probe,pump_early_green_candle_sniper,pump_early_sniper_research,pump_early_research_rank_canary,pump_early_birth_probe,pump_early_birth_probe_micro_canary,pump_early_late_momentum_watch",
    )
    ML_TRAIN_ALLOW_MISSING_ENTRY_LANE: bool = _bool_env("ML_TRAIN_ALLOW_MISSING_ENTRY_LANE", True)
    ML_TRAIN_DEX_ALLOWLIST: str = os.getenv("ML_TRAIN_DEX_ALLOWLIST", "")
    ML_BOOTSTRAP_RESEARCH_SHADOW_ENABLED: bool = _bool_env("ML_BOOTSTRAP_RESEARCH_SHADOW_ENABLED", True)
    ML_BOOTSTRAP_ONLY_WHEN_MODEL_MISSING: bool = _bool_env("ML_BOOTSTRAP_ONLY_WHEN_MODEL_MISSING", False)
    ML_BOOTSTRAP_ENTRY_LANE_ALLOWLIST: str = os.getenv(
        "ML_BOOTSTRAP_ENTRY_LANE_ALLOWLIST",
        "pump_early_pumpswap_profit,pump_early_pumpswap_prime,pump_early_pumpswap_rebound_prime,pump_early_meteor_prime,pump_early_pumpswap_breakout_probe,pump_early_green_candle_sniper,pump_early_sniper_research,pump_early_research_rank_canary,pump_early_birth_probe,pump_early_birth_probe_micro_canary,pump_early_late_momentum_watch",
    )
    ML_BOOTSTRAP_DEX_ALLOWLIST: str = os.getenv("ML_BOOTSTRAP_DEX_ALLOWLIST", "")
    PUMP_EARLY_SHADOW_RECOVERY_ENABLED: bool = _bool_env("PUMP_EARLY_SHADOW_RECOVERY_ENABLED", True)
    PUMP_EARLY_SHADOW_RECOVERY_WINDOW: int = _num_env("PUMP_EARLY_SHADOW_RECOVERY_WINDOW", int, 8)
    PUMP_EARLY_SHADOW_RECOVERY_MIN_TRADES: int = _num_env("PUMP_EARLY_SHADOW_RECOVERY_MIN_TRADES", int, 8)
    PUMP_EARLY_SHADOW_RECOVERY_MIN_AVG_PNL_PCT: float = _num_env(
        "PUMP_EARLY_SHADOW_RECOVERY_MIN_AVG_PNL_PCT",
        float,
        5.0,
    )
    PUMP_EARLY_SHADOW_RECOVERY_MIN_WIN_RATE_PCT: float = _num_env(
        "PUMP_EARLY_SHADOW_RECOVERY_MIN_WIN_RATE_PCT",
        float,
        45.0,
    )
    PUMP_EARLY_SHADOW_RECOVERY_MAX_SEVERE_EXITS: int = _num_env(
        "PUMP_EARLY_SHADOW_RECOVERY_MAX_SEVERE_EXITS",
        int,
        2,
    )
    PUMP_EARLY_SHADOW_RECOVERY_MAX_LIQ_CRUSH: int = _num_env(
        "PUMP_EARLY_SHADOW_RECOVERY_MAX_LIQ_CRUSH",
        int,
        1,
    )
    PUMP_EARLY_SHADOW_RECOVERY_MAX_CONSECUTIVE_LOSSES: int = _num_env(
        "PUMP_EARLY_SHADOW_RECOVERY_MAX_CONSECUTIVE_LOSSES",
        int,
        3,
    )
    PUMP_EARLY_SHADOW_RECOVERY_MAX_AGE_H: float = _num_env("PUMP_EARLY_SHADOW_RECOVERY_MAX_AGE_H", float, 36.0)

    # ------- endpoints ---------------------------------------------
    RPC_URL: str = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    DEXSCREENER_API: str = os.getenv("DEXSCREENER_API") or os.getenv("DEX_API_BASE") or "https://api.dexscreener.com"

    # ------- Helius -------------------------------------------------
    HELIUS_API_KEY: str | None = os.getenv("HELIUS_API_KEY")
    HELIUS_REST_BASE: str = os.getenv("HELIUS_REST_BASE", os.getenv("HELIUS_API_BASE", "https://api.helius.xyz"))
    HELIUS_RPC_URL: str = os.getenv(
        "HELIUS_RPC_URL",
        f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else RPC_URL,
    )
    USE_PRIVATE_RPC_FIRST: bool = _bool_env("USE_PRIVATE_RPC_FIRST", True)
    SOL_RPC_FALLBACKS: Tuple[str, ...] = _csv_tuple(os.getenv("SOL_RPC_FALLBACKS", ""), lower=False)

    # ------- otros servicios ---------------------------------------
    RUGCHECK_API_BASE: str = os.getenv("RUGCHECK_API_BASE", "https://api.rugcheck.xyz/v1")
    RUGCHECK_API_KEY: str | None = os.getenv("RUGCHECK_API_KEY")
    BITQUERY_TOKEN: str | None = os.getenv("BITQUERY_TOKEN")
    PUMPFUN_PROGRAM: str | None = os.getenv("PUMPFUN_PROGRAM")

    # ------- GeckoTerminal -----------------------------------------
    USE_GECKO_TERMINAL: bool = _bool_env("USE_GECKO_TERMINAL", True)
    GECKO_API_URL: str = os.getenv("GECKO_API_URL", "https://api.geckoterminal.com/api/v2")
    GECKO_SOL_ENDPOINT: str = f"{GECKO_API_URL}/networks/solana/pools"

    # ------- Jupiter Price v3 (Lite) -------------------------------
    USE_JUPITER_PRICE: bool = _bool_env("USE_JUPITER_PRICE", True)
    JUPITER_PRICE_URL: str = os.getenv("JUPITER_PRICE_URL", "https://lite-api.jup.ag/price/v3")
    JUPITER_RPM: int = _num_env("JUPITER_RPM", int, 60)
    JUPITER_TTL_NIL_SHORT: int = _num_env("JUPITER_TTL_NIL_SHORT", int, 120)
    JUPITER_TTL_NIL_MAX: int = _num_env("JUPITER_TTL_NIL_MAX", int, 600)
    JUPITER_TTL_OK: int = _num_env("JUPITER_TTL_OK", int, 120)

    # ------- Impacto Jupiter (router opcional) ---------------------
    USE_JUPITER_IMPACT: bool = _bool_env("USE_JUPITER_IMPACT", False)
    IMPACT_PROBE_SOL: float = _num_env("IMPACT_PROBE_SOL", float, 0.05)
    IMPACT_MAX_PCT: float = _num_env("IMPACT_MAX_PCT", float, 8.0)
    JUP_MANAGED_ENABLED: bool = _bool_env("JUP_MANAGED_ENABLED", True)
    JUP_LEGACY_SWAP_ENABLED: bool = _bool_env("JUP_LEGACY_SWAP_ENABLED", True)
    JUP_ORDER_URL: str = os.getenv("JUP_ORDER_URL", "https://api.jup.ag/ultra/v1/order")
    JUP_EXECUTE_URL: str = os.getenv("JUP_EXECUTE_URL", "https://api.jup.ag/ultra/v1/execute")
    JUP_MANAGED_SLIPPAGE_BPS: int = _num_env("JUP_MANAGED_SLIPPAGE_BPS", int, 100)
    JITO_BLOCK_ENGINE_URL: str = os.getenv("JITO_BLOCK_ENGINE_URL", "https://ny.mainnet.block-engine.jito.wtf")
    JITO_UUID: str | None = os.getenv("JITO_UUID")
    JITO_BROADCAST_ENABLED: bool = _bool_env("JITO_BROADCAST_ENABLED", False)
    JITO_BUNDLE_ONLY: bool = _bool_env("JITO_BUNDLE_ONLY", True)

    # ------- filtros básicos ---------------------------------------
    MAX_AGE_DAYS: float = _num_env("MAX_AGE_DAYS", float, 2.0)
    MIN_AGE_MIN: float = _num_env("MIN_AGE_MIN", float, 3.0)
    MIN_HOLDERS: int = _num_env("MIN_HOLDERS", int, 10)
    MIN_LIQUIDITY_USD: float = _num_env("MIN_LIQUIDITY_USD", float, 5_000.0)
    MIN_VOL_USD_24H: float = _num_env("MIN_VOL_USD_24H", float, 10_000.0)
    MIN_MARKET_CAP_USD: float = _num_env("MIN_MARKET_CAP_USD", float, 5_000.0)
    MAX_MARKET_CAP_USD: float = _num_env("MAX_MARKET_CAP_USD", float, 20_000.0)
    MAX_24H_VOLUME: float = _num_env("MAX_24H_VOLUME", float, 1_500_000.0)
    MIN_SCORE_TOTAL: int = _num_env("MIN_SCORE_TOTAL", int, 50)
    MAX_ACTIVE_POSITIONS: int = _num_env("MAX_ACTIVE_POSITIONS", int, 25)
    MAX_TRADE_AMOUNT_SOL: float = _num_env("MAX_TRADE_AMOUNT_SOL", float, TRADE_AMOUNT_SOL)
    LIVE_MAX_DAILY_BUYS: int = _num_env("LIVE_MAX_DAILY_BUYS", int, 10)
    LIVE_MAX_DAILY_LOSS_SOL: float = _num_env("LIVE_MAX_DAILY_LOSS_SOL", float, 0.5)
    LIVE_MAX_CONSECUTIVE_LOSSES: int = _num_env("LIVE_MAX_CONSECUTIVE_LOSSES", int, 4)
    LIVE_DISABLE_BUYS_ON_RPC_ERRORS: bool = _bool_env("LIVE_DISABLE_BUYS_ON_RPC_ERRORS", True)
    LIVE_DISABLE_BUYS_ON_MODEL_MISSING: bool = _bool_env("LIVE_DISABLE_BUYS_ON_MODEL_MISSING", False)
    LIVE_DISABLE_BUYS_ON_MODEL_DEGRADED: bool = _bool_env("LIVE_DISABLE_BUYS_ON_MODEL_DEGRADED", False)
    DEXS_TXNS_5M_MIN: int = _num_env("DEXS_TXNS_5M_MIN", int, 2)
    FILTER_PROFILE_BY_DISCOVERY: bool = _bool_env("FILTER_PROFILE_BY_DISCOVERY", False)
    SNAPSHOT_QUALITY_FILTER_ENABLED: bool = _bool_env("SNAPSHOT_QUALITY_FILTER_ENABLED", False)
    SNAPSHOT_MAX_MISSING_FIELDS: int = _num_env("SNAPSHOT_MAX_MISSING_FIELDS", int, 99)
    SNAPSHOT_REQUIRE_ACTIVITY_SIGNAL: bool = _bool_env("SNAPSHOT_REQUIRE_ACTIVITY_SIGNAL", False)
    SNAPSHOT_REQUIRE_SOCIAL_OR_TREND: bool = _bool_env("SNAPSHOT_REQUIRE_SOCIAL_OR_TREND", False)
    SNAPSHOT_REQUIRE_RUG_SCORE: bool = _bool_env("SNAPSHOT_REQUIRE_RUG_SCORE", False)
    SNAPSHOT_ALLOWED_PRICE_SOURCES: Tuple[str, ...] = _csv_tuple(os.getenv("SNAPSHOT_ALLOWED_PRICE_SOURCES", ""))
    TOXIC_INITIAL_SELL_PRESSURE_TTL_S: int = _num_env("TOXIC_INITIAL_SELL_PRESSURE_TTL_S", int, 900)

    # Overrides opcionales por regimen (solo se aplican si FILTER_PROFILE_BY_DISCOVERY=true)
    DEX_MIN_AGE_MIN: float | None = _opt_num_env("DEX_MIN_AGE_MIN", float)
    DEX_MIN_HOLDERS: int | None = _opt_num_env("DEX_MIN_HOLDERS", int)
    DEX_MIN_LIQUIDITY_USD: float | None = _opt_num_env("DEX_MIN_LIQUIDITY_USD", float)
    DEX_MIN_VOL_USD_24H: float | None = _opt_num_env("DEX_MIN_VOL_USD_24H", float)
    DEX_MIN_MARKET_CAP_USD: float | None = _opt_num_env("DEX_MIN_MARKET_CAP_USD", float)
    DEX_MAX_MARKET_CAP_USD: float | None = _opt_num_env("DEX_MAX_MARKET_CAP_USD", float)
    DEX_BUY_SOFT_SCORE_MIN: int | None = _opt_num_env("DEX_BUY_SOFT_SCORE_MIN", int)
    DEX_AI_THRESHOLD: float | None = _opt_num_env("DEX_AI_THRESHOLD", float)
    DEX_REQUIRE_JUPITER_FOR_BUY: bool | None = _opt_bool_env("DEX_REQUIRE_JUPITER_FOR_BUY")

    PUMPFUN_MIN_AGE_MIN: float | None = _opt_num_env("PUMPFUN_MIN_AGE_MIN", float)
    PUMPFUN_MIN_HOLDERS: int | None = _opt_num_env("PUMPFUN_MIN_HOLDERS", int)
    PUMPFUN_MIN_LIQUIDITY_USD: float | None = _opt_num_env("PUMPFUN_MIN_LIQUIDITY_USD", float)
    PUMPFUN_MIN_VOL_USD_24H: float | None = _opt_num_env("PUMPFUN_MIN_VOL_USD_24H", float)
    PUMPFUN_MIN_MARKET_CAP_USD: float | None = _opt_num_env("PUMPFUN_MIN_MARKET_CAP_USD", float)
    PUMPFUN_MAX_MARKET_CAP_USD: float | None = _opt_num_env("PUMPFUN_MAX_MARKET_CAP_USD", float)
    PUMPFUN_BUY_SOFT_SCORE_MIN: int | None = _opt_num_env("PUMPFUN_BUY_SOFT_SCORE_MIN", int)
    PUMPFUN_AI_THRESHOLD: float | None = _opt_num_env("PUMPFUN_AI_THRESHOLD", float)
    PUMPFUN_REQUIRE_JUPITER_FOR_BUY: bool | None = _opt_bool_env("PUMPFUN_REQUIRE_JUPITER_FOR_BUY")

    REVIVAL_MIN_AGE_MIN: float | None = _opt_num_env("REVIVAL_MIN_AGE_MIN", float)
    REVIVAL_MIN_HOLDERS: int | None = _opt_num_env("REVIVAL_MIN_HOLDERS", int)
    REVIVAL_MIN_LIQUIDITY_USD: float | None = _opt_num_env("REVIVAL_MIN_LIQUIDITY_USD", float)
    REVIVAL_MIN_VOL_USD_24H: float | None = _opt_num_env("REVIVAL_MIN_VOL_USD_24H", float)
    REVIVAL_MIN_MARKET_CAP_USD: float | None = _opt_num_env("REVIVAL_MIN_MARKET_CAP_USD", float)
    REVIVAL_MAX_MARKET_CAP_USD: float | None = _opt_num_env("REVIVAL_MAX_MARKET_CAP_USD", float)
    REVIVAL_BUY_SOFT_SCORE_MIN: int | None = _opt_num_env("REVIVAL_BUY_SOFT_SCORE_MIN", int)
    REVIVAL_AI_THRESHOLD: float | None = _opt_num_env("REVIVAL_AI_THRESHOLD", float)
    REVIVAL_REQUIRE_JUPITER_FOR_BUY: bool | None = _opt_bool_env("REVIVAL_REQUIRE_JUPITER_FOR_BUY")

    # ------- regimenes operativos / sizing -------------------------
    REGIME_PUMP_EARLY_MAX_AGE_MIN: float = _num_env("REGIME_PUMP_EARLY_MAX_AGE_MIN", float, 5.0)
    DYNAMIC_SIZING_ENABLED: bool = _bool_env("DYNAMIC_SIZING_ENABLED", False)
    AI_SIZING_ENABLED: bool = _bool_env("AI_SIZING_ENABLED", False)
    SIZE_MIN_MULTIPLIER: float = _num_env("SIZE_MIN_MULTIPLIER", float, 0.10)
    SIZE_MID_MULTIPLIER: float = _num_env("SIZE_MID_MULTIPLIER", float, 0.20)
    SIZE_MAX_MULTIPLIER: float = _num_env("SIZE_MAX_MULTIPLIER", float, 0.20)
    SIZE_ACCEPTABLE_MIN_POINTS: int = _num_env("SIZE_ACCEPTABLE_MIN_POINTS", int, 3)
    SIZE_PREMIUM_MIN_POINTS: int = _num_env("SIZE_PREMIUM_MIN_POINTS", int, 6)
    PUMP_EARLY_MAX_SIZE_MULTIPLIER: float = _num_env("PUMP_EARLY_MAX_SIZE_MULTIPLIER", float, 0.50)
    DEX_MATURE_MAX_SIZE_MULTIPLIER: float = _num_env("DEX_MATURE_MAX_SIZE_MULTIPLIER", float, 1.00)
    REVIVAL_MAX_SIZE_MULTIPLIER: float = _num_env("REVIVAL_MAX_SIZE_MULTIPLIER", float, 0.75)
    MAX_ACTIVE_POSITIONS_PER_REGIME: int = _num_env("MAX_ACTIVE_POSITIONS_PER_REGIME", int, 0)
    PUMP_EARLY_MAX_ACTIVE_POSITIONS: int | None = _opt_num_env("PUMP_EARLY_MAX_ACTIVE_POSITIONS", int)
    DEX_MATURE_MAX_ACTIVE_POSITIONS: int | None = _opt_num_env("DEX_MATURE_MAX_ACTIVE_POSITIONS", int)
    REVIVAL_MAX_ACTIVE_POSITIONS: int | None = _opt_num_env("REVIVAL_MAX_ACTIVE_POSITIONS", int)
    STRATEGY_REGIME_MODE_DEFAULT: str = (
        (os.getenv("STRATEGY_REGIME_MODE_DEFAULT", "shadow") or "shadow").strip().lower()
    )
    PUMP_EARLY_EXECUTION_MODE: str = (
        (os.getenv("PUMP_EARLY_EXECUTION_MODE", STRATEGY_REGIME_MODE_DEFAULT) or STRATEGY_REGIME_MODE_DEFAULT)
        .strip()
        .lower()
    )
    DEX_MATURE_EXECUTION_MODE: str = (
        (os.getenv("DEX_MATURE_EXECUTION_MODE", "live") or "live").strip().lower()
    )
    REVIVAL_EXECUTION_MODE: str = (
        (os.getenv("REVIVAL_EXECUTION_MODE", STRATEGY_REGIME_MODE_DEFAULT) or STRATEGY_REGIME_MODE_DEFAULT)
        .strip()
        .lower()
    )
    PAPER_AGGRESSIVE_TRADING_ENABLED: bool = _bool_env("PAPER_AGGRESSIVE_TRADING_ENABLED", False)
    PAPER_AGGRESSIVE_CONFIRM_SNAPSHOTS: int = _num_env("PAPER_AGGRESSIVE_CONFIRM_SNAPSHOTS", int, 1)
    PAPER_AGGRESSIVE_CONFIRM_BACKOFF_S: int = _num_env("PAPER_AGGRESSIVE_CONFIRM_BACKOFF_S", int, 10)
    PAPER_AGGRESSIVE_MIN_AGE_MIN: float = _num_env("PAPER_AGGRESSIVE_MIN_AGE_MIN", float, 0.05)
    PAPER_AGGRESSIVE_MIN_LIQUIDITY_USD: float = _num_env("PAPER_AGGRESSIVE_MIN_LIQUIDITY_USD", float, 1500.0)
    PAPER_AGGRESSIVE_MIN_MARKET_CAP_USD: float = _num_env("PAPER_AGGRESSIVE_MIN_MARKET_CAP_USD", float, 2000.0)
    PAPER_AGGRESSIVE_MAX_MARKET_CAP_USD: float = _num_env("PAPER_AGGRESSIVE_MAX_MARKET_CAP_USD", float, 500_000.0)
    PAPER_AGGRESSIVE_MIN_SCORE_TOTAL: int = _num_env("PAPER_AGGRESSIVE_MIN_SCORE_TOTAL", int, 30)
    PAPER_AGGRESSIVE_MIN_RANK_SCORE: float = _num_env("PAPER_AGGRESSIVE_MIN_RANK_SCORE", float, 35.0)
    PAPER_AGGRESSIVE_MIN_TXNS_5M: int = _num_env("PAPER_AGGRESSIVE_MIN_TXNS_5M", int, 3)
    PAPER_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS: int = _num_env(
        "PAPER_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS",
        int,
        5,
    )
    PAPER_AGGRESSIVE_MAX_PRICE_IMPACT_PCT: float = _num_env(
        "PAPER_AGGRESSIVE_MAX_PRICE_IMPACT_PCT",
        float,
        20.0,
    )
    PAPER_AGGRESSIVE_REQUIRE_ROUTE: bool = _bool_env("PAPER_AGGRESSIVE_REQUIRE_ROUTE", True)
    PAPER_AGGRESSIVE_REQUIRE_PRICE: bool = _bool_env("PAPER_AGGRESSIVE_REQUIRE_PRICE", True)
    PAPER_AGGRESSIVE_BUY_RESEARCH_LANES: bool = _bool_env("PAPER_AGGRESSIVE_BUY_RESEARCH_LANES", False)
    LIVE_AGGRESSIVE_TRADING_ENABLED: bool = _bool_env("LIVE_AGGRESSIVE_TRADING_ENABLED", False)
    LIVE_AGGRESSIVE_CONFIRM_SNAPSHOTS: int = _num_env("LIVE_AGGRESSIVE_CONFIRM_SNAPSHOTS", int, 1)
    LIVE_AGGRESSIVE_CONFIRM_BACKOFF_S: int = _num_env("LIVE_AGGRESSIVE_CONFIRM_BACKOFF_S", int, 10)
    LIVE_AGGRESSIVE_MIN_AGE_MIN: float = _num_env("LIVE_AGGRESSIVE_MIN_AGE_MIN", float, 0.05)
    LIVE_AGGRESSIVE_MIN_LIQUIDITY_USD: float = _num_env("LIVE_AGGRESSIVE_MIN_LIQUIDITY_USD", float, 1500.0)
    LIVE_AGGRESSIVE_MIN_MARKET_CAP_USD: float = _num_env("LIVE_AGGRESSIVE_MIN_MARKET_CAP_USD", float, 2000.0)
    LIVE_AGGRESSIVE_MAX_MARKET_CAP_USD: float = _num_env("LIVE_AGGRESSIVE_MAX_MARKET_CAP_USD", float, 500_000.0)
    LIVE_AGGRESSIVE_MIN_SCORE_TOTAL: int = _num_env("LIVE_AGGRESSIVE_MIN_SCORE_TOTAL", int, 30)
    LIVE_AGGRESSIVE_MIN_RANK_SCORE: float = _num_env("LIVE_AGGRESSIVE_MIN_RANK_SCORE", float, 35.0)
    LIVE_AGGRESSIVE_MIN_TXNS_5M: int = _num_env("LIVE_AGGRESSIVE_MIN_TXNS_5M", int, 3)
    LIVE_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS: int = _num_env(
        "LIVE_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS",
        int,
        5,
    )
    LIVE_AGGRESSIVE_MAX_PRICE_IMPACT_PCT: float = _num_env(
        "LIVE_AGGRESSIVE_MAX_PRICE_IMPACT_PCT",
        float,
        20.0,
    )
    LIVE_AGGRESSIVE_REQUIRE_ROUTE: bool = _bool_env("LIVE_AGGRESSIVE_REQUIRE_ROUTE", True)
    LIVE_AGGRESSIVE_REQUIRE_PRICE: bool = _bool_env("LIVE_AGGRESSIVE_REQUIRE_PRICE", True)
    LIVE_AGGRESSIVE_BUY_RESEARCH_LANES: bool = _bool_env("LIVE_AGGRESSIVE_BUY_RESEARCH_LANES", False)
    LIVE_AGGRESSIVE_CONTINUE_ON_HEALTH: bool = _bool_env("LIVE_AGGRESSIVE_CONTINUE_ON_HEALTH", False)
    LIVE_AGGRESSIVE_HEALTH_SIZE_CAP_MULTIPLIER: float = _num_env(
        "LIVE_AGGRESSIVE_HEALTH_SIZE_CAP_MULTIPLIER",
        float,
        0.10,
    )
    STRATEGY_CONFIRMATION_ENABLED: bool = _bool_env("STRATEGY_CONFIRMATION_ENABLED", True)
    STRATEGY_CONFIRM_DEFAULT_SNAPSHOTS: int = _num_env("STRATEGY_CONFIRM_DEFAULT_SNAPSHOTS", int, 2)
    STRATEGY_CONFIRM_DEFAULT_BACKOFF_S: int = _num_env("STRATEGY_CONFIRM_DEFAULT_BACKOFF_S", int, 45)
    STRATEGY_CONFIRM_REQUIRE_ROUTE: bool = _bool_env("STRATEGY_CONFIRM_REQUIRE_ROUTE", True)
    STRATEGY_CONFIRM_LIQUIDITY_DROP_PCT: float = _num_env("STRATEGY_CONFIRM_LIQUIDITY_DROP_PCT", float, 20.0)
    PUMP_EARLY_CONFIRM_SNAPSHOTS: int = _num_env("PUMP_EARLY_CONFIRM_SNAPSHOTS", int, 3)
    DEX_MATURE_CONFIRM_SNAPSHOTS: int = _num_env("DEX_MATURE_CONFIRM_SNAPSHOTS", int, 2)
    REVIVAL_CONFIRM_SNAPSHOTS: int = _num_env("REVIVAL_CONFIRM_SNAPSHOTS", int, 2)
    PUMP_EARLY_CONFIRM_BACKOFF_S: int = _num_env("PUMP_EARLY_CONFIRM_BACKOFF_S", int, 30)
    DEX_MATURE_CONFIRM_BACKOFF_S: int = _num_env("DEX_MATURE_CONFIRM_BACKOFF_S", int, 45)
    REVIVAL_CONFIRM_BACKOFF_S: int = _num_env("REVIVAL_CONFIRM_BACKOFF_S", int, 60)
    PUMP_EARLY_CONFIRM_MIN_AGE_MIN: float = _num_env("PUMP_EARLY_CONFIRM_MIN_AGE_MIN", float, 1.0)
    DEX_MATURE_CONFIRM_MIN_AGE_MIN: float = _num_env("DEX_MATURE_CONFIRM_MIN_AGE_MIN", float, 3.0)
    REVIVAL_CONFIRM_MIN_AGE_MIN: float = _num_env("REVIVAL_CONFIRM_MIN_AGE_MIN", float, 8.0)
    PUMP_EARLY_QUALITY_MIN_POINTS: int = _num_env("PUMP_EARLY_QUALITY_MIN_POINTS", int, 0)
    PUMP_EARLY_QUALITY_BACKOFF_S: int = _num_env("PUMP_EARLY_QUALITY_BACKOFF_S", int, 120)
    PUMP_EARLY_QUALITY_MIN_AGE_MIN: float = _num_env("PUMP_EARLY_QUALITY_MIN_AGE_MIN", float, 0.0)
    PUMP_EARLY_QUALITY_MIN_LIQUIDITY_USD: float = _num_env("PUMP_EARLY_QUALITY_MIN_LIQUIDITY_USD", float, 0.0)
    PUMP_EARLY_QUALITY_MIN_VOLUME_USD_24H: float = _num_env("PUMP_EARLY_QUALITY_MIN_VOLUME_USD_24H", float, 0.0)
    PUMP_EARLY_QUALITY_MIN_MARKET_CAP_USD: float = _num_env("PUMP_EARLY_QUALITY_MIN_MARKET_CAP_USD", float, 0.0)
    PUMP_EARLY_QUALITY_MIN_HOLDERS: int = _num_env("PUMP_EARLY_QUALITY_MIN_HOLDERS", int, 0)
    PUMP_EARLY_QUALITY_MIN_SCORE_TOTAL: int = _num_env("PUMP_EARLY_QUALITY_MIN_SCORE_TOTAL", int, 0)
    PUMP_EARLY_QUALITY_MAX_PRICE_IMPACT_PCT: float = _num_env(
        "PUMP_EARLY_QUALITY_MAX_PRICE_IMPACT_PCT",
        float,
        0.0,
    )
    PUMP_EARLY_LIVE_HARD_MAX_MARKET_CAP_USD: float = _num_env(
        "PUMP_EARLY_LIVE_HARD_MAX_MARKET_CAP_USD",
        float,
        125_000.0,
    )
    PUMP_EARLY_LIVE_HARD_MAX_PRICE_IMPACT_PCT: float = _num_env(
        "PUMP_EARLY_LIVE_HARD_MAX_PRICE_IMPACT_PCT",
        float,
        10.0,
    )
    PUMP_EARLY_LIVE_MAX_SNAPSHOT_MISSING_FIELDS: int = _num_env(
        "PUMP_EARLY_LIVE_MAX_SNAPSHOT_MISSING_FIELDS",
        int,
        3,
    )
    PAPER_COLD_START_ENABLED: bool = _bool_env("PAPER_COLD_START_ENABLED", True)
    PAPER_COLD_START_MAX_CLOSED_TRADES: int = _num_env("PAPER_COLD_START_MAX_CLOSED_TRADES", int, 50)
    PAPER_COLD_START_MIN_AGE_MIN: float = _num_env("PAPER_COLD_START_MIN_AGE_MIN", float, 12.0)
    PAPER_COLD_START_MIN_SCORE_TOTAL: int = _num_env("PAPER_COLD_START_MIN_SCORE_TOTAL", int, 45)
    PAPER_COLD_START_MIN_LIQUIDITY_USD: float = _num_env("PAPER_COLD_START_MIN_LIQUIDITY_USD", float, 10_000.0)
    PAPER_COLD_START_MIN_MARKET_CAP_USD: float = _num_env("PAPER_COLD_START_MIN_MARKET_CAP_USD", float, 15_000.0)
    PAPER_COLD_START_MAX_SNAPSHOT_MISSING_FIELDS: int = _num_env(
        "PAPER_COLD_START_MAX_SNAPSHOT_MISSING_FIELDS",
        int,
        4,
    )
    PAPER_COLD_START_MIN_RANK_SCORE: float = _num_env("PAPER_COLD_START_MIN_RANK_SCORE", float, 12.5)
    PAPER_COLD_START_REQUIRE_PRICE_PCT_5M: bool = _bool_env("PAPER_COLD_START_REQUIRE_PRICE_PCT_5M", True)
    PAPER_COLD_START_MIN_PRICE_PCT_5M: float = _num_env("PAPER_COLD_START_MIN_PRICE_PCT_5M", float, 0.0)
    PAPER_COLD_START_MAX_PRICE_PCT_5M: float = _num_env("PAPER_COLD_START_MAX_PRICE_PCT_5M", float, 80.0)
    PAPER_COLD_START_SHADOW_PROBE_ENABLED: bool = _bool_env("PAPER_COLD_START_SHADOW_PROBE_ENABLED", True)
    PAPER_COLD_START_SHADOW_PROBE_SIZE_MULTIPLIER: float = _num_env(
        "PAPER_COLD_START_SHADOW_PROBE_SIZE_MULTIPLIER",
        float,
        0.10,
    )
    PUMP_EARLY_SNIPER_ENABLED: bool = _bool_env("PUMP_EARLY_SNIPER_ENABLED", True)
    PUMP_EARLY_SNIPER_MODE: str = (
        (os.getenv("PUMP_EARLY_SNIPER_MODE", "canary_aggressive") or "canary_aggressive").strip().lower()
    )
    PUMP_EARLY_SNIPER_MIN_AGE_MIN: float = _num_env("PUMP_EARLY_SNIPER_MIN_AGE_MIN", float, 3.0)
    PUMP_EARLY_SNIPER_MAX_AGE_MIN: float = _num_env("PUMP_EARLY_SNIPER_MAX_AGE_MIN", float, 30.0)
    PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD: float = _num_env(
        "PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD",
        float,
        1_500.0,
    )
    PUMP_EARLY_SNIPER_MICRO_MIN_LIQUIDITY_USD: float = _num_env(
        "PUMP_EARLY_SNIPER_MICRO_MIN_LIQUIDITY_USD",
        float,
        1_000.0,
    )
    PUMP_EARLY_SNIPER_MICRO_MIN_VOLUME_USD_24H: float = _num_env(
        "PUMP_EARLY_SNIPER_MICRO_MIN_VOLUME_USD_24H",
        float,
        15_000.0,
    )
    PUMP_EARLY_SNIPER_MIN_MARKET_CAP_USD: float = _num_env(
        "PUMP_EARLY_SNIPER_MIN_MARKET_CAP_USD",
        float,
        2_000.0,
    )
    PUMP_EARLY_SNIPER_MAX_MARKET_CAP_USD: float = _num_env(
        "PUMP_EARLY_SNIPER_MAX_MARKET_CAP_USD",
        float,
        200_000.0,
    )
    PUMP_EARLY_SNIPER_MICRO_MAX_MARKET_CAP_USD: float = _num_env(
        "PUMP_EARLY_SNIPER_MICRO_MAX_MARKET_CAP_USD",
        float,
        125_000.0,
    )
    PUMP_EARLY_SNIPER_MIN_SCORE_TOTAL: int = _num_env("PUMP_EARLY_SNIPER_MIN_SCORE_TOTAL", int, 30)
    PUMP_EARLY_SNIPER_MICRO_MIN_SCORE_TOTAL: int = _num_env(
        "PUMP_EARLY_SNIPER_MICRO_MIN_SCORE_TOTAL",
        int,
        25,
    )
    PUMP_EARLY_SNIPER_MIN_RANK_SCORE: float = _num_env(
        "PUMP_EARLY_SNIPER_MIN_RANK_SCORE",
        float,
        40.0,
    )
    PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE: float = _num_env(
        "PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE",
        float,
        42.0,
    )
    PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT: float = _num_env(
        "PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT",
        float,
        20.0,
    )
    PUMP_EARLY_SNIPER_MICRO_MAX_PRICE_IMPACT_PCT: float = _num_env(
        "PUMP_EARLY_SNIPER_MICRO_MAX_PRICE_IMPACT_PCT",
        float,
        15.0,
    )
    PUMP_EARLY_SNIPER_MIN_TXNS_5M: int = _num_env("PUMP_EARLY_SNIPER_MIN_TXNS_5M", int, 15)
    PUMP_EARLY_SNIPER_MICRO_MIN_TXNS_5M: int = _num_env(
        "PUMP_EARLY_SNIPER_MICRO_MIN_TXNS_5M",
        int,
        50,
    )
    PUMP_EARLY_SNIPER_MIN_PRICE_PCT_5M: float = _num_env(
        "PUMP_EARLY_SNIPER_MIN_PRICE_PCT_5M",
        float,
        -20.0,
    )
    PUMP_EARLY_SNIPER_MAX_PRICE_PCT_5M: float = _num_env(
        "PUMP_EARLY_SNIPER_MAX_PRICE_PCT_5M",
        float,
        240.0,
    )
    PUMP_EARLY_SNIPER_MICRO_MIN_PRICE_PCT_5M: float = _num_env(
        "PUMP_EARLY_SNIPER_MICRO_MIN_PRICE_PCT_5M",
        float,
        5.0,
    )
    PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS: int = _num_env(
        "PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS",
        int,
        5,
    )
    PUMP_EARLY_SNIPER_HOT_MIN_RANK_SCORE: float = _num_env(
        "PUMP_EARLY_SNIPER_HOT_MIN_RANK_SCORE",
        float,
        50.0,
    )
    PUMP_EARLY_SNIPER_HOT_MIN_TXNS_5M: int = _num_env("PUMP_EARLY_SNIPER_HOT_MIN_TXNS_5M", int, 100)
    PUMP_EARLY_SNIPER_HOT_MIN_PRICE_PCT_5M: float = _num_env(
        "PUMP_EARLY_SNIPER_HOT_MIN_PRICE_PCT_5M",
        float,
        10.0,
    )
    PUMP_EARLY_SNIPER_HOT_MAX_PRICE_PCT_5M: float = _num_env(
        "PUMP_EARLY_SNIPER_HOT_MAX_PRICE_PCT_5M",
        float,
        120.0,
    )
    PUMP_EARLY_SNIPER_HOT_MAX_SNAPSHOT_MISSING_FIELDS: int = _num_env(
        "PUMP_EARLY_SNIPER_HOT_MAX_SNAPSHOT_MISSING_FIELDS",
        int,
        2,
    )
    PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_AGE_MIN: float = _num_env(
        "PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_AGE_MIN",
        float,
        3.0,
    )
    PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_TXNS_5M: int = _num_env(
        "PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_TXNS_5M",
        int,
        40,
    )
    PUMP_EARLY_SNIPER_FAST_CONFIRM_BACKOFF_S: int = _num_env(
        "PUMP_EARLY_SNIPER_FAST_CONFIRM_BACKOFF_S",
        int,
        10,
    )
    PUMP_EARLY_SNIPER_SIZE_MICRO_MULTIPLIER: float = _num_env(
        "PUMP_EARLY_SNIPER_SIZE_MICRO_MULTIPLIER",
        float,
        0.10,
    )
    PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER: float = _num_env(
        "PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER",
        float,
        0.20,
    )
    PUMP_EARLY_SNIPER_SIZE_HOT_MULTIPLIER: float = _num_env(
        "PUMP_EARLY_SNIPER_SIZE_HOT_MULTIPLIER",
        float,
        0.30,
    )
    PUMP_EARLY_SNIPER_CANARY_INITIAL_CLOSES: int = _num_env(
        "PUMP_EARLY_SNIPER_CANARY_INITIAL_CLOSES",
        int,
        10,
    )
    PUMP_EARLY_SNIPER_CANARY_INITIAL_SIZE_CAP: float = _num_env(
        "PUMP_EARLY_SNIPER_CANARY_INITIAL_SIZE_CAP",
        float,
        0.20,
    )
    PUMP_EARLY_SNIPER_MAX_OPEN_PAPER: int = _num_env("PUMP_EARLY_SNIPER_MAX_OPEN_PAPER", int, 3)
    PUMP_EARLY_SNIPER_MAX_OPEN_LIVE_CANARY: int = _num_env(
        "PUMP_EARLY_SNIPER_MAX_OPEN_LIVE_CANARY",
        int,
        1,
    )
    PUMP_EARLY_SNIPER_MAX_OPEN_LIVE_CANARY_ADVANCED: int = _num_env(
        "PUMP_EARLY_SNIPER_MAX_OPEN_LIVE_CANARY_ADVANCED",
        int,
        2,
    )
    PUMP_EARLY_SNIPER_ADVANCED_MIN_CLOSED: int = _num_env(
        "PUMP_EARLY_SNIPER_ADVANCED_MIN_CLOSED",
        int,
        10,
    )
    PUMP_EARLY_SNIPER_ADVANCED_MIN_AVG_PNL_PCT: float = _num_env(
        "PUMP_EARLY_SNIPER_ADVANCED_MIN_AVG_PNL_PCT",
        float,
        1.0,
    )
    PUMP_EARLY_SNIPER_ADVANCED_MAX_LOSS_STREAK: int = _num_env(
        "PUMP_EARLY_SNIPER_ADVANCED_MAX_LOSS_STREAK",
        int,
        3,
    )
    PUMP_EARLY_SNIPER_DEMOTE_LOSS_STREAK: int = _num_env(
        "PUMP_EARLY_SNIPER_DEMOTE_LOSS_STREAK",
        int,
        4,
    )
    PUMP_EARLY_SNIPER_DEMOTE_WINDOW_TRADES: int = _num_env(
        "PUMP_EARLY_SNIPER_DEMOTE_WINDOW_TRADES",
        int,
        8,
    )
    PUMP_EARLY_SNIPER_DEMOTE_AVG_PNL_PCT: float = _num_env(
        "PUMP_EARLY_SNIPER_DEMOTE_AVG_PNL_PCT",
        float,
        -5.0,
    )
    PUMP_EARLY_SNIPER_DEMOTE_LIQ_CRUSH_FIRST_CLOSES: int = _num_env(
        "PUMP_EARLY_SNIPER_DEMOTE_LIQ_CRUSH_FIRST_CLOSES",
        int,
        10,
    )
    PUMP_EARLY_SNIPER_DEMOTE_LIQ_CRUSH_ROLLING: int = _num_env(
        "PUMP_EARLY_SNIPER_DEMOTE_LIQ_CRUSH_ROLLING",
        int,
        2,
    )
    PUMP_EARLY_SNIPER_RECOVERY_MIN_PAPER_CLOSES: int = _num_env(
        "PUMP_EARLY_SNIPER_RECOVERY_MIN_PAPER_CLOSES",
        int,
        8,
    )
    PUMP_EARLY_SNIPER_RECOVERY_MIN_AVG_PNL_PCT: float = _num_env(
        "PUMP_EARLY_SNIPER_RECOVERY_MIN_AVG_PNL_PCT",
        float,
        2.0,
    )
    PUMP_EARLY_SNIPER_PAPER_CONTINUE_ON_HEALTH: bool = _bool_env(
        "PUMP_EARLY_SNIPER_PAPER_CONTINUE_ON_HEALTH",
        False,
    )
    PUMP_EARLY_SNIPER_PAPER_RECOVERY_SIZE_CAP: float = _num_env(
        "PUMP_EARLY_SNIPER_PAPER_RECOVERY_SIZE_CAP",
        float,
        0.20,
    )
    PUMP_EARLY_SNIPER_LIVE_CONTINUE_ON_HEALTH: bool = _bool_env(
        "PUMP_EARLY_SNIPER_LIVE_CONTINUE_ON_HEALTH",
        True,
    )
    PUMP_EARLY_SNIPER_LIVE_RECOVERY_SIZE_CAP: float = _num_env(
        "PUMP_EARLY_SNIPER_LIVE_RECOVERY_SIZE_CAP",
        float,
        0.10,
    )
    PUMP_EARLY_SNIPER_LIVE_REQUIRE_MANUAL_APPROVAL: bool = _bool_env(
        "PUMP_EARLY_SNIPER_LIVE_REQUIRE_MANUAL_APPROVAL",
        True,
    )
    PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_ENABLED: bool = _bool_env(
        "PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_ENABLED",
        True,
    )
    PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_MIN_AGE_MIN: float = _num_env(
        "PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_MIN_AGE_MIN",
        float,
        3.0,
    )
    PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_USD: float = _num_env(
        "PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_USD",
        float,
        1_500.0,
    )
    PUMP_EARLY_PROFIT_LANE_ENABLED: bool = _bool_env("PUMP_EARLY_PROFIT_LANE_ENABLED", True)
    PUMP_EARLY_PROFIT_DEX_ALLOWLIST: str = os.getenv(
        "PUMP_EARLY_PROFIT_DEX_ALLOWLIST",
        "pumpswap",
    )
    PUMP_EARLY_PROFIT_REQUIRE_REAL_LIQUIDITY: bool = _bool_env(
        "PUMP_EARLY_PROFIT_REQUIRE_REAL_LIQUIDITY",
        True,
    )
    PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD: float = _num_env(
        "PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD",
        float,
        5_000.0,
    )
    PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL: int = _num_env("PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL", int, 35)
    PUMP_EARLY_PROFIT_MIN_AGE_MIN: float = _num_env("PUMP_EARLY_PROFIT_MIN_AGE_MIN", float, 3.0)
    PUMP_EARLY_PROFIT_MAX_AGE_MIN: float = _num_env("PUMP_EARLY_PROFIT_MAX_AGE_MIN", float, 30.0)
    PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT",
        float,
        10.0,
    )
    PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD: float = _num_env(
        "PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD",
        float,
        0.0,
    )
    PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD: float = _num_env(
        "PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD",
        float,
        0.0,
    )
    PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES: str = os.getenv(
        "PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES",
        "300:999",
    )
    PUMP_EARLY_AGGRESSIVE_RESEARCH_GUARD_ENABLED: bool = _bool_env(
        "PUMP_EARLY_AGGRESSIVE_RESEARCH_GUARD_ENABLED",
        True,
    )
    PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PRICE5M_RANGES: str = os.getenv(
        "PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PRICE5M_RANGES",
        "300:999",
    )
    PUMP_EARLY_AGGRESSIVE_RESEARCH_DEX_ALLOWLIST: str = os.getenv(
        "PUMP_EARLY_AGGRESSIVE_RESEARCH_DEX_ALLOWLIST",
        "pumpswap",
    )
    PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_HIGH_MCAP_USD: float = _num_env(
        "PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_HIGH_MCAP_USD",
        float,
        100_000.0,
    )
    PUMP_EARLY_AGGRESSIVE_RESEARCH_HIGH_MCAP_ALLOW_MIN_TXNS_5M: int = _num_env(
        "PUMP_EARLY_AGGRESSIVE_RESEARCH_HIGH_MCAP_ALLOW_MIN_TXNS_5M",
        int,
        1_200,
    )
    PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PROXY: bool = _bool_env(
        "PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PROXY",
        True,
    )
    PUMP_EARLY_AGGRESSIVE_RESEARCH_HOT_PRICE5M_MIN_PCT: float = _num_env(
        "PUMP_EARLY_AGGRESSIVE_RESEARCH_HOT_PRICE5M_MIN_PCT",
        float,
        180.0,
    )
    PUMP_EARLY_AGGRESSIVE_RESEARCH_HOT_MIN_TXNS_5M: int = _num_env(
        "PUMP_EARLY_AGGRESSIVE_RESEARCH_HOT_MIN_TXNS_5M",
        int,
        150,
    )
    HOT_QUEUE_ENABLED: bool = _bool_env("HOT_QUEUE_ENABLED", True)
    HOT_QUEUE_MAX_SIZE: int = _num_env("HOT_QUEUE_MAX_SIZE", int, 1000)
    HOT_QUEUE_BATCH_SIZE: int = _num_env("HOT_QUEUE_BATCH_SIZE", int, 30)
    HOT_QUEUE_MAX_AGE_MIN: float = _num_env("HOT_QUEUE_MAX_AGE_MIN", float, 20.0)
    HOT_QUEUE_HIGH_PRIORITY_MIN_SCORE: float = _num_env("HOT_QUEUE_HIGH_PRIORITY_MIN_SCORE", float, 75.0)
    HOT_QUEUE_LOW_PRIORITY_MAX_AGE_MIN: float = _num_env("HOT_QUEUE_LOW_PRIORITY_MAX_AGE_MIN", float, 5.0)
    HOT_QUEUE_HIGH_PRIORITY_MAX_AGE_MIN: float = _num_env("HOT_QUEUE_HIGH_PRIORITY_MAX_AGE_MIN", float, 20.0)
    HOT_QUEUE_DYNAMIC_BATCH_ENABLED: bool = _bool_env("HOT_QUEUE_DYNAMIC_BATCH_ENABLED", True)
    HOT_QUEUE_DEDUP_TTL_S: int = _num_env("HOT_QUEUE_DEDUP_TTL_S", int, 1800)
    HOT_QUEUE_PRIORITY_SOURCES: str = os.getenv("HOT_QUEUE_PRIORITY_SOURCES", "pumpportal,pumpfun")
    FAST_ENRICHMENT_ENABLED: bool = _bool_env("FAST_ENRICHMENT_ENABLED", True)
    FAST_ENRICHMENT_TIMEOUT_S: float = _num_env("FAST_ENRICHMENT_TIMEOUT_S", float, 3.0)
    FAST_ENRICHMENT_REQUIRE_LEVEL_FOR_GREEN_SNIPER: int = _num_env(
        "FAST_ENRICHMENT_REQUIRE_LEVEL_FOR_GREEN_SNIPER",
        int,
        1,
    )
    FAST_ENRICHMENT_ALLOW_MISSING_RUG: bool = _bool_env("FAST_ENRICHMENT_ALLOW_MISSING_RUG", True)
    FAST_ENRICHMENT_ALLOW_MISSING_SOCIALS: bool = _bool_env("FAST_ENRICHMENT_ALLOW_MISSING_SOCIALS", True)
    FAST_ENRICHMENT_ALLOW_MISSING_HOLDERS: bool = _bool_env("FAST_ENRICHMENT_ALLOW_MISSING_HOLDERS", True)
    BUY_FLOW_SCHEDULER_ENABLED: bool = _bool_env("BUY_FLOW_SCHEDULER_ENABLED", True)
    HOT_LOOP_SLEEP_S: float = _num_env("HOT_LOOP_SLEEP_S", float, 1.0)
    HOT_LOOP_BATCH_SIZE: int = _num_env("HOT_LOOP_BATCH_SIZE", int, 8)
    NORMAL_LOOP_SLEEP_S: float = _num_env("NORMAL_LOOP_SLEEP_S", float, 5.0)
    NORMAL_LOOP_BATCH_SIZE: int = _num_env("NORMAL_LOOP_BATCH_SIZE", int, 20)
    MONITOR_LOOP_SLEEP_S: float = _num_env("MONITOR_LOOP_SLEEP_S", float, 3.0)
    GREEN_SNIPER_ENABLED: bool = _bool_env("GREEN_SNIPER_ENABLED", True)
    GREEN_SNIPER_MIN_AGE_MIN: float = _num_env("GREEN_SNIPER_MIN_AGE_MIN", float, 0.15)
    GREEN_SNIPER_MAX_AGE_MIN: float = _num_env("GREEN_SNIPER_MAX_AGE_MIN", float, 8.0)
    GREEN_SNIPER_MIN_LIQUIDITY_USD: float = _num_env("GREEN_SNIPER_MIN_LIQUIDITY_USD", float, 1200.0)
    GREEN_SNIPER_MIN_MARKET_CAP_USD: float = _num_env("GREEN_SNIPER_MIN_MARKET_CAP_USD", float, 2000.0)
    GREEN_SNIPER_MAX_MARKET_CAP_USD: float = _num_env("GREEN_SNIPER_MAX_MARKET_CAP_USD", float, 180000.0)
    GREEN_SNIPER_MIN_PRICE_PCT_5M: float = _num_env("GREEN_SNIPER_MIN_PRICE_PCT_5M", float, 20.0)
    GREEN_SNIPER_MAX_PRICE_PCT_5M: float = _num_env("GREEN_SNIPER_MAX_PRICE_PCT_5M", float, 280.0)
    GREEN_SNIPER_MIN_TXNS_5M: int = _num_env("GREEN_SNIPER_MIN_TXNS_5M", int, 35)
    GREEN_SNIPER_HOT_MIN_TXNS_5M: int = _num_env("GREEN_SNIPER_HOT_MIN_TXNS_5M", int, 80)
    GREEN_SNIPER_MIN_BUY_SELL_RATIO: float = _num_env("GREEN_SNIPER_MIN_BUY_SELL_RATIO", float, 1.15)
    GREEN_SNIPER_MAX_PRICE_IMPACT_PCT: float = _num_env("GREEN_SNIPER_MAX_PRICE_IMPACT_PCT", float, 20.0)
    GREEN_SNIPER_ALLOW_PROXY_LIQUIDITY_PAPER: bool = _bool_env("GREEN_SNIPER_ALLOW_PROXY_LIQUIDITY_PAPER", True)
    GREEN_SNIPER_REQUIRE_ROUTE_PAPER: bool = _bool_env("GREEN_SNIPER_REQUIRE_ROUTE_PAPER", False)
    GREEN_SNIPER_RANK_GUARD_ENABLED: bool = _bool_env("GREEN_SNIPER_RANK_GUARD_ENABLED", True)
    GREEN_SNIPER_RANK_GUARD_MIN_SCORE: float = _num_env("GREEN_SNIPER_RANK_GUARD_MIN_SCORE", float, 45.0)
    GREEN_SNIPER_RANK_GUARD_BYPASS_PAPER_BIRTH_PROBE: bool = _bool_env(
        "GREEN_SNIPER_RANK_GUARD_BYPASS_PAPER_BIRTH_PROBE",
        False,
    )
    GREEN_SNIPER_PAPER_BIRTH_PROBE_ENABLED: bool = _bool_env("GREEN_SNIPER_PAPER_BIRTH_PROBE_ENABLED", True)
    GREEN_SNIPER_PAPER_BIRTH_PROBE_SHADOW_FIRST: bool = _bool_env(
        "GREEN_SNIPER_PAPER_BIRTH_PROBE_SHADOW_FIRST",
        True,
    )
    GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_AGE_MIN: float = _num_env(
        "GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_AGE_MIN",
        float,
        3.0,
    )
    GREEN_SNIPER_PAPER_BIRTH_PROBE_MIN_LIQUIDITY_USD: float = _num_env(
        "GREEN_SNIPER_PAPER_BIRTH_PROBE_MIN_LIQUIDITY_USD",
        float,
        1000.0,
    )
    GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_PRICE_IMPACT_PCT: float = _num_env(
        "GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_PRICE_IMPACT_PCT",
        float,
        25.0,
    )
    BIRTH_PROBE_MICRO_CANARY_ENABLED: bool = _bool_env("BIRTH_PROBE_MICRO_CANARY_ENABLED", True)
    BIRTH_PROBE_MICRO_CANARY_PAPER_ENABLED: bool = _bool_env("BIRTH_PROBE_MICRO_CANARY_PAPER_ENABLED", True)
    BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED: bool = _bool_env("BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED", False)
    BIRTH_PROBE_MICRO_CANARY_AMOUNT_SOL: float = _num_env("BIRTH_PROBE_MICRO_CANARY_AMOUNT_SOL", float, 0.01)
    BIRTH_PROBE_MICRO_CANARY_MAX_OPEN: int = _num_env("BIRTH_PROBE_MICRO_CANARY_MAX_OPEN", int, 1)
    BIRTH_PROBE_MICRO_CANARY_MAX_DAILY_BUYS: int = _num_env("BIRTH_PROBE_MICRO_CANARY_MAX_DAILY_BUYS", int, 5)
    BIRTH_PROBE_MICRO_CANARY_ALLOWED_REASON_GROUPS: str = os.getenv(
        "BIRTH_PROBE_MICRO_CANARY_ALLOWED_REASON_GROUPS",
        "paper_birth_probe_proxy_low_txns,paper_birth_probe_low_green_proxy_low_txns",
    )
    BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_EV_PCT: float = _num_env(
        "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_EV_PCT",
        float,
        5.0,
    )
    BIRTH_PROBE_MICRO_CANARY_PNL_CAP_PCT: float = _num_env(
        "BIRTH_PROBE_MICRO_CANARY_PNL_CAP_PCT",
        float,
        1000.0,
    )
    BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_CAPPED_EV_PCT: float = _num_env(
        "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_CAPPED_EV_PCT",
        float,
        -1.0,
    )
    BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_SAMPLES: int = _num_env(
        "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_SAMPLES",
        int,
        50,
    )
    BIRTH_PROBE_MICRO_CANARY_TIME_STOP_MIN: float = _num_env(
        "BIRTH_PROBE_MICRO_CANARY_TIME_STOP_MIN",
        float,
        3.0,
    )
    BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_EXIT_MIN: float = _num_env(
        "BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_EXIT_MIN",
        float,
        2.0,
    )
    BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_MIN_PNL: float = _num_env(
        "BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_MIN_PNL",
        float,
        5.0,
    )
    BIRTH_PROBE_MICRO_CANARY_TP1_PCT: float = _num_env("BIRTH_PROBE_MICRO_CANARY_TP1_PCT", float, 25.0)
    BIRTH_PROBE_MICRO_CANARY_TP1_FRACTION: float = _num_env(
        "BIRTH_PROBE_MICRO_CANARY_TP1_FRACTION",
        float,
        0.50,
    )
    BIRTH_PROBE_MICRO_CANARY_TP2_PCT: float = _num_env("BIRTH_PROBE_MICRO_CANARY_TP2_PCT", float, 100.0)
    BIRTH_PROBE_MICRO_CANARY_TP2_FRACTION: float = _num_env(
        "BIRTH_PROBE_MICRO_CANARY_TP2_FRACTION",
        float,
        0.20,
    )
    BIRTH_PROBE_MICRO_CANARY_TP3_PCT: float = _num_env("BIRTH_PROBE_MICRO_CANARY_TP3_PCT", float, 300.0)
    BIRTH_PROBE_MICRO_CANARY_TP3_FRACTION: float = _num_env(
        "BIRTH_PROBE_MICRO_CANARY_TP3_FRACTION",
        float,
        0.20,
    )
    BIRTH_PROBE_MICRO_CANARY_TP4_PCT: float = _num_env("BIRTH_PROBE_MICRO_CANARY_TP4_PCT", float, 700.0)
    BIRTH_PROBE_MICRO_CANARY_TP4_FRACTION: float = _num_env(
        "BIRTH_PROBE_MICRO_CANARY_TP4_FRACTION",
        float,
        0.15,
    )
    BIRTH_PROBE_MICRO_CANARY_MOONBAG_FRACTION: float = _num_env(
        "BIRTH_PROBE_MICRO_CANARY_MOONBAG_FRACTION",
        float,
        0.30,
    )
    GREEN_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS: int = _num_env(
        "GREEN_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS",
        int,
        6,
    )
    GREEN_SNIPER_LIVE_ENABLED: bool = _bool_env("GREEN_SNIPER_LIVE_ENABLED", False)
    GREEN_SNIPER_REQUIRE_ROUTE_LIVE: bool = _bool_env("GREEN_SNIPER_REQUIRE_ROUTE_LIVE", True)
    GREEN_SNIPER_LIVE_MIN_AGE_MIN: float = _num_env("GREEN_SNIPER_LIVE_MIN_AGE_MIN", float, 0.35)
    GREEN_SNIPER_LIVE_MAX_AGE_MIN: float = _num_env("GREEN_SNIPER_LIVE_MAX_AGE_MIN", float, 6.0)
    GREEN_SNIPER_LIVE_MIN_LIQUIDITY_USD: float = _num_env("GREEN_SNIPER_LIVE_MIN_LIQUIDITY_USD", float, 2500.0)
    GREEN_SNIPER_LIVE_MAX_PRICE_IMPACT_PCT: float = _num_env("GREEN_SNIPER_LIVE_MAX_PRICE_IMPACT_PCT", float, 12.0)
    GREEN_SNIPER_LIVE_MIN_TXNS_5M: int = _num_env("GREEN_SNIPER_LIVE_MIN_TXNS_5M", int, 60)
    GREEN_SNIPER_LIVE_SIZE_SOL: float = _num_env("GREEN_SNIPER_LIVE_SIZE_SOL", float, 0.01)
    GREEN_SNIPER_LIVE_MAX_OPEN: int = _num_env("GREEN_SNIPER_LIVE_MAX_OPEN", int, 1)
    GREEN_SNIPER_MAX_OPEN_PAPER: int = _num_env("GREEN_SNIPER_MAX_OPEN_PAPER", int, 6)
    GREEN_SNIPER_LIVE_MAX_DAILY_BUYS: int = _num_env("GREEN_SNIPER_LIVE_MAX_DAILY_BUYS", int, 3)
    GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL: float = _num_env("GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL", float, 0.05)
    GREEN_SNIPER_LIVE_MAX_CONSECUTIVE_LOSSES: int = _num_env("GREEN_SNIPER_LIVE_MAX_CONSECUTIVE_LOSSES", int, 2)
    GREEN_SNIPER_LIVE_DISABLE_ON_LIQ_CRUSH: bool = _bool_env("GREEN_SNIPER_LIVE_DISABLE_ON_LIQ_CRUSH", True)
    GREEN_SNIPER_PAPER_ROUTE_PROXY_ENABLED: bool = _bool_env("GREEN_SNIPER_PAPER_ROUTE_PROXY_ENABLED", True)
    GREEN_SNIPER_PAPER_ROUTE_PROXY_MIN_LIQUIDITY_USD: float = _num_env(
        "GREEN_SNIPER_PAPER_ROUTE_PROXY_MIN_LIQUIDITY_USD",
        float,
        1200.0,
    )
    GREEN_SNIPER_ROUTE_WAIT_MAX_S: float = _num_env("GREEN_SNIPER_ROUTE_WAIT_MAX_S", float, 8.0)
    GREEN_SNIPER_ROUTE_RETRY_INTERVAL_S: float = _num_env("GREEN_SNIPER_ROUTE_RETRY_INTERVAL_S", float, 2.0)
    PAPER_SNIPER_MODE: bool = _bool_env("PAPER_SNIPER_MODE", False)
    PAPER_SNIPER_IGNORE_REGIME_COOLDOWN: bool = _bool_env("PAPER_SNIPER_IGNORE_REGIME_COOLDOWN", True)
    PAPER_SNIPER_CONTINUE_ON_HEALTH: bool = _bool_env("PAPER_SNIPER_CONTINUE_ON_HEALTH", True)
    PAPER_SNIPER_BUY_GREEN_SNIPER: bool = _bool_env("PAPER_SNIPER_BUY_GREEN_SNIPER", True)
    PAPER_SNIPER_BUY_BREAKOUT: bool = _bool_env("PAPER_SNIPER_BUY_BREAKOUT", True)
    PAPER_SNIPER_SHADOW_REJECTS: bool = _bool_env("PAPER_SNIPER_SHADOW_REJECTS", True)
    GREEN_SNIPER_REJECT_SHADOW_ENABLED: bool = _bool_env("GREEN_SNIPER_REJECT_SHADOW_ENABLED", True)
    GREEN_SNIPER_REJECT_SHADOW_MAX_OPEN: int = _num_env("GREEN_SNIPER_REJECT_SHADOW_MAX_OPEN", int, 20)
    GREEN_SNIPER_REJECT_SHADOW_MAX_AGE_MIN: float = _num_env("GREEN_SNIPER_REJECT_SHADOW_MAX_AGE_MIN", float, 15.0)
    GREEN_SNIPER_SIZE_MODE: str = (os.getenv("GREEN_SNIPER_SIZE_MODE", "fixed_tiers") or "fixed_tiers").strip().lower()
    GREEN_SNIPER_SIZE_MICRO_SOL: float = _num_env("GREEN_SNIPER_SIZE_MICRO_SOL", float, 0.10)
    GREEN_SNIPER_SIZE_CORE_SOL: float = _num_env("GREEN_SNIPER_SIZE_CORE_SOL", float, 0.10)
    GREEN_SNIPER_SIZE_HOT_SOL: float = _num_env("GREEN_SNIPER_SIZE_HOT_SOL", float, 0.10)
    GREEN_SNIPER_LIVE_SIZE_MODE: str = (
        os.getenv("GREEN_SNIPER_LIVE_SIZE_MODE", "canary_fixed") or "canary_fixed"
    ).strip().lower()
    GREEN_SNIPER_LIVE_ADVANCED_SIZE_SOL: float = _num_env("GREEN_SNIPER_LIVE_ADVANCED_SIZE_SOL", float, 0.03)
    GREEN_SNIPER_LIVE_ADVANCED_ENABLED: bool = _bool_env("GREEN_SNIPER_LIVE_ADVANCED_ENABLED", False)
    GREEN_SNIPER_ML_MODE: str = (os.getenv("GREEN_SNIPER_ML_MODE", "sizing_only") or "sizing_only").strip().lower()
    GREEN_SNIPER_ML_BLOCK_ENABLED: bool = _bool_env("GREEN_SNIPER_ML_BLOCK_ENABLED", False)
    GREEN_SNIPER_ML_RISK_REDUCE_SIZE: bool = _bool_env("GREEN_SNIPER_ML_RISK_REDUCE_SIZE", True)
    GREEN_SNIPER_ML_EV_SIZE_UP_PAPER: bool = _bool_env("GREEN_SNIPER_ML_EV_SIZE_UP_PAPER", True)
    GREEN_SNIPER_ML_EV_SIZE_UP_LIVE: bool = _bool_env("GREEN_SNIPER_ML_EV_SIZE_UP_LIVE", False)
    RESEARCH_RANK_CANARY_ENABLED: bool = _bool_env("RESEARCH_RANK_CANARY_ENABLED", True)
    RESEARCH_RANK_CANARY_PAPER_ENABLED: bool = _bool_env("RESEARCH_RANK_CANARY_PAPER_ENABLED", True)
    RESEARCH_RANK_CANARY_LIVE_ENABLED: bool = _bool_env("RESEARCH_RANK_CANARY_LIVE_ENABLED", False)
    RESEARCH_RANK_CANARY_MIN_SCORE: float = _num_env("RESEARCH_RANK_CANARY_MIN_SCORE", float, 64.81)
    RESEARCH_RANK_CANARY_SIZE_SOL: float = _num_env("RESEARCH_RANK_CANARY_SIZE_SOL", float, 0.01)
    RESEARCH_RANK_CANARY_MAX_OPEN: int = _num_env("RESEARCH_RANK_CANARY_MAX_OPEN", int, 1)
    RESEARCH_RANK_CANARY_MAX_DAILY_BUYS: int = _num_env("RESEARCH_RANK_CANARY_MAX_DAILY_BUYS", int, 3)
    RESEARCH_RANK_CANARY_FORCE_OWN_LANE: bool = _bool_env("RESEARCH_RANK_CANARY_FORCE_OWN_LANE", True)
    RESEARCH_RANK_CANARY_SHADOW_IF_NOT_EXECUTABLE: bool = _bool_env(
        "RESEARCH_RANK_CANARY_SHADOW_IF_NOT_EXECUTABLE",
        True,
    )
    RESEARCH_RANK_CANARY_REQUIRE_ROUTE_PAPER: bool = _bool_env("RESEARCH_RANK_CANARY_REQUIRE_ROUTE_PAPER", True)
    RESEARCH_RANK_CANARY_REQUIRE_ROUTE_LIVE: bool = _bool_env("RESEARCH_RANK_CANARY_REQUIRE_ROUTE_LIVE", True)
    RESEARCH_RANK_CANARY_MIN_LIQUIDITY_USD: float = _num_env("RESEARCH_RANK_CANARY_MIN_LIQUIDITY_USD", float, 2000.0)
    RESEARCH_RANK_CANARY_PREFER_REAL_LIQUIDITY: bool = _bool_env("RESEARCH_RANK_CANARY_PREFER_REAL_LIQUIDITY", True)
    RESEARCH_RANK_CANARY_MIN_TXNS_5M: int = _num_env("RESEARCH_RANK_CANARY_MIN_TXNS_5M", int, 300)
    RESEARCH_RANK_CANARY_MIN_MCAP_USD: float = _num_env("RESEARCH_RANK_CANARY_MIN_MCAP_USD", float, 25_000.0)
    RESEARCH_RANK_CANARY_MAX_MCAP_USD: float = _num_env("RESEARCH_RANK_CANARY_MAX_MCAP_USD", float, 100_000.0)
    RESEARCH_RANK_CANARY_MIN_PRICE5M: float = _num_env("RESEARCH_RANK_CANARY_MIN_PRICE5M", float, 40.0)
    RESEARCH_RANK_CANARY_MAX_PRICE5M: float = _num_env("RESEARCH_RANK_CANARY_MAX_PRICE5M", float, 100.0)
    RESEARCH_RANK_CANARY_LOW_BAND_MIN_RANK_SCORE: float = _num_env(
        "RESEARCH_RANK_CANARY_LOW_BAND_MIN_RANK_SCORE",
        float,
        70.0,
    )
    RESEARCH_RANK_CANARY_LOW_BAND_MIN_LIQUIDITY_USD: float = _num_env(
        "RESEARCH_RANK_CANARY_LOW_BAND_MIN_LIQUIDITY_USD",
        float,
        20_000.0,
    )
    RESEARCH_RANK_CANARY_PRIORITY_BONUS: float = _num_env("RESEARCH_RANK_CANARY_PRIORITY_BONUS", float, 25.0)
    RESEARCH_RANK_CANARY_PRIORITY_MODE: bool = _bool_env("RESEARCH_RANK_CANARY_PRIORITY_MODE", True)
    RESEARCH_RANK_CANARY_PRIORITY_MIN_TXNS_5M: int = _num_env(
        "RESEARCH_RANK_CANARY_PRIORITY_MIN_TXNS_5M",
        int,
        1000,
    )
    RESEARCH_RANK_CANARY_PRIORITY_MIN_LIQUIDITY_USD: float = _num_env(
        "RESEARCH_RANK_CANARY_PRIORITY_MIN_LIQUIDITY_USD",
        float,
        15_000.0,
    )
    RESEARCH_RANK_CANARY_PRIORITY_MIN_PRICE5M: float = _num_env(
        "RESEARCH_RANK_CANARY_PRIORITY_MIN_PRICE5M",
        float,
        50.0,
    )
    RESEARCH_RANK_CANARY_PRIORITY_MAX_PRICE5M: float = _num_env(
        "RESEARCH_RANK_CANARY_PRIORITY_MAX_PRICE5M",
        float,
        120.0,
    )
    RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE: float = _num_env(
        "RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE",
        float,
        70.0,
    )
    RESEARCH_RANK_CANARY_PRIORITY_MAX_OPEN: int = _num_env(
        "RESEARCH_RANK_CANARY_PRIORITY_MAX_OPEN",
        int,
        2,
    )
    PAPER_EXPLORATION_QUOTA_ENABLED: bool = _bool_env("PAPER_EXPLORATION_QUOTA_ENABLED", True)
    PAPER_EXPLORATION_MAX_DAILY_BUYS: int = _num_env("PAPER_EXPLORATION_MAX_DAILY_BUYS", int, 5)
    PAPER_EXPLORATION_MAX_OPEN: int = _num_env("PAPER_EXPLORATION_MAX_OPEN", int, 1)
    PAPER_EXPLORATION_AMOUNT_SOL: float = _num_env("PAPER_EXPLORATION_AMOUNT_SOL", float, 0.005)
    PAPER_EXPLORATION_IDLE_HOURS: float = _num_env("PAPER_EXPLORATION_IDLE_HOURS", float, 4.0)
    GREEN_SNIPER_RISK_GUARD_ENABLED: bool = _bool_env("GREEN_SNIPER_RISK_GUARD_ENABLED", True)
    GREEN_SNIPER_LIQ_GUARD_ENABLED: bool = _bool_env("GREEN_SNIPER_LIQ_GUARD_ENABLED", True)
    GREEN_SNIPER_LIQ_PROXY_MAX_PRICE5M: float = _num_env("GREEN_SNIPER_LIQ_PROXY_MAX_PRICE5M", float, 100.0)
    GREEN_SNIPER_LIQ_PROXY_MIN_TXNS_FOR_EXCEPTION: int = _num_env("GREEN_SNIPER_LIQ_PROXY_MIN_TXNS_FOR_EXCEPTION", int, 300)
    GREEN_SNIPER_REAL_LIQ_MIN_FOR_HOT: float = _num_env("GREEN_SNIPER_REAL_LIQ_MIN_FOR_HOT", float, 2500.0)
    GREEN_SNIPER_POLICY_MODE: str = (os.getenv("GREEN_SNIPER_POLICY_MODE", "shadow") or "shadow").strip().lower()
    GREEN_SNIPER_BUY_RESTRICTED_ENABLED: bool = _bool_env("GREEN_SNIPER_BUY_RESTRICTED_ENABLED", True)
    GREEN_SNIPER_RESTRICTED_MIN_RANK: float = _num_env("GREEN_SNIPER_RESTRICTED_MIN_RANK", float, 64.0)
    GREEN_SNIPER_RESTRICTED_MIN_TXNS: int = _num_env("GREEN_SNIPER_RESTRICTED_MIN_TXNS", int, 300)
    GREEN_SNIPER_RESTRICTED_MIN_LIQUIDITY: float = _num_env("GREEN_SNIPER_RESTRICTED_MIN_LIQUIDITY", float, 10_000.0)
    GREEN_SNIPER_RESTRICTED_MIN_MCAP: float = _num_env("GREEN_SNIPER_RESTRICTED_MIN_MCAP", float, 25_000.0)
    GREEN_SNIPER_RESTRICTED_MAX_MCAP: float = _num_env("GREEN_SNIPER_RESTRICTED_MAX_MCAP", float, 100_000.0)
    GREEN_SNIPER_RESTRICTED_MIN_PRICE5M: float = _num_env("GREEN_SNIPER_RESTRICTED_MIN_PRICE5M", float, 25.0)
    GREEN_SNIPER_RESTRICTED_MAX_PRICE5M: float = _num_env("GREEN_SNIPER_RESTRICTED_MAX_PRICE5M", float, 100.0)
    GREEN_SNIPER_RESTRICTED_REQUIRE_ROUTE: bool = _bool_env("GREEN_SNIPER_RESTRICTED_REQUIRE_ROUTE", True)
    GREEN_SNIPER_RESTRICTED_MAX_PRICE_IMPACT_PCT: float = _num_env(
        "GREEN_SNIPER_RESTRICTED_MAX_PRICE_IMPACT_PCT",
        float,
        12.0,
    )
    GREEN_SNIPER_RESTRICTED_REQUIRE_PROVIDER_HEALTH: bool = _bool_env(
        "GREEN_SNIPER_RESTRICTED_REQUIRE_PROVIDER_HEALTH",
        True,
    )
    GREEN_SNIPER_LIQ_CRUSH_SHADOW_IN_PAPER: bool = _bool_env("GREEN_SNIPER_LIQ_CRUSH_SHADOW_IN_PAPER", True)
    GREEN_SNIPER_EARLY_DUMP_ENABLED: bool = _bool_env("GREEN_SNIPER_EARLY_DUMP_ENABLED", True)
    GREEN_SNIPER_EARLY_DUMP_AFTER_S: int = _num_env("GREEN_SNIPER_EARLY_DUMP_AFTER_S", int, 35)
    GREEN_SNIPER_EARLY_DUMP_PNL_PCT: float = _num_env("GREEN_SNIPER_EARLY_DUMP_PNL_PCT", float, -12.0)
    GREEN_SNIPER_EARLY_DUMP_CONFIRM_TICKS: int = _num_env("GREEN_SNIPER_EARLY_DUMP_CONFIRM_TICKS", int, 2)
    GREEN_SNIPER_EARLY_DUMP_IGNORE_IF_PEAK_PCT: float = _num_env("GREEN_SNIPER_EARLY_DUMP_IGNORE_IF_PEAK_PCT", float, 15.0)
    RESEARCH_RANK_CANARY_EARLY_DUMP_ENABLED: bool = _bool_env("RESEARCH_RANK_CANARY_EARLY_DUMP_ENABLED", True)
    RESEARCH_RANK_CANARY_EARLY_DUMP_AFTER_S: int = _num_env("RESEARCH_RANK_CANARY_EARLY_DUMP_AFTER_S", int, 35)
    RESEARCH_RANK_CANARY_EARLY_DUMP_PNL_PCT: float = _num_env("RESEARCH_RANK_CANARY_EARLY_DUMP_PNL_PCT", float, -12.0)
    RESEARCH_RANK_CANARY_EARLY_DUMP_CONFIRM_TICKS: int = _num_env(
        "RESEARCH_RANK_CANARY_EARLY_DUMP_CONFIRM_TICKS",
        int,
        2,
    )
    RESEARCH_RANK_CANARY_EARLY_DUMP_IGNORE_IF_PEAK_PCT: float = _num_env(
        "RESEARCH_RANK_CANARY_EARLY_DUMP_IGNORE_IF_PEAK_PCT",
        float,
        15.0,
    )
    EARLY_DUMP_CUT_PNL_THRESHOLDS: str = os.getenv("EARLY_DUMP_CUT_PNL_THRESHOLDS", "-8,-10,-12")
    EARLY_DUMP_CUT_AFTER_S_VALUES: str = os.getenv("EARLY_DUMP_CUT_AFTER_S_VALUES", "25,35,45")
    EARLY_DUMP_CUT_CONFIRM_TICKS_VALUES: str = os.getenv("EARLY_DUMP_CUT_CONFIRM_TICKS_VALUES", "1,2")
    EARLY_DUMP_CUT_IGNORE_IF_PEAK_VALUES: str = os.getenv("EARLY_DUMP_CUT_IGNORE_IF_PEAK_VALUES", "10,15,20")
    GREEN_SNIPER_PASS_HEALTH_ENABLED: bool = _bool_env("GREEN_SNIPER_PASS_HEALTH_ENABLED", True)
    GREEN_SNIPER_PASS_MIN_TRADES: int = _num_env("GREEN_SNIPER_PASS_MIN_TRADES", int, 20)
    GREEN_SNIPER_PASS_MIN_AVG_PNL_PCT: float = _num_env("GREEN_SNIPER_PASS_MIN_AVG_PNL_PCT", float, 0.0)
    GREEN_SNIPER_PASS_NEGATIVE_ACTION: str = os.getenv("GREEN_SNIPER_PASS_NEGATIVE_ACTION", "shadow")
    LATE_MOMENTUM_WATCH_ENABLED: bool = _bool_env("LATE_MOMENTUM_WATCH_ENABLED", True)
    LATE_MOMENTUM_WATCH_MIN_PRICE5M: float = _num_env("LATE_MOMENTUM_WATCH_MIN_PRICE5M", float, 300.0)
    LATE_MOMENTUM_WATCH_MAX_PRICE5M: float = _num_env("LATE_MOMENTUM_WATCH_MAX_PRICE5M", float, 750.0)
    LATE_MOMENTUM_WATCH_MIN_RANK_SCORE: float = _num_env("LATE_MOMENTUM_WATCH_MIN_RANK_SCORE", float, 55.0)
    LATE_MOMENTUM_WATCH_MIN_TXNS_5M: int = _num_env("LATE_MOMENTUM_WATCH_MIN_TXNS_5M", int, 300)
    LATE_MOMENTUM_WATCH_MIN_LIQUIDITY_USD: float = _num_env("LATE_MOMENTUM_WATCH_MIN_LIQUIDITY_USD", float, 2000.0)
    LATE_MOMENTUM_WATCH_MAX_PRICE_IMPACT_PCT: float = _num_env("LATE_MOMENTUM_WATCH_MAX_PRICE_IMPACT_PCT", float, 12.0)
    LATE_MOMENTUM_WATCH_ALLOW_RANK_MISSING_PAPER: bool = _bool_env("LATE_MOMENTUM_WATCH_ALLOW_RANK_MISSING_PAPER", True)
    LATE_MOMENTUM_WATCH_BUY_ENABLED: bool = _bool_env("LATE_MOMENTUM_WATCH_BUY_ENABLED", False)
    LATE_MOMENTUM_WATCH_RESEARCH_ENABLED: bool = _bool_env("LATE_MOMENTUM_WATCH_RESEARCH_ENABLED", True)
    LATE_MOMENTUM_WATCH_AUTORESEARCH_ENABLED: bool = _bool_env("LATE_MOMENTUM_WATCH_AUTORESEARCH_ENABLED", False)
    LATE_MOMENTUM_WATCH_PAPER_CANARY_ENABLED: bool = _bool_env("LATE_MOMENTUM_WATCH_PAPER_CANARY_ENABLED", False)
    LATE_MOMENTUM_WATCH_LIVE_ENABLED: bool = _bool_env("LATE_MOMENTUM_WATCH_LIVE_ENABLED", False)
    LATE_MOMENTUM_WATCH_MAX_OPEN_PAPER: int = _num_env("LATE_MOMENTUM_WATCH_MAX_OPEN_PAPER", int, 1)
    LATE_MOMENTUM_WATCH_MAX_OPEN_LIVE: int = _num_env("LATE_MOMENTUM_WATCH_MAX_OPEN_LIVE", int, 0)
    ML_GREEN_SNIPER_BLOCK_ENABLED: bool = _bool_env("ML_GREEN_SNIPER_BLOCK_ENABLED", False)
    SOCIALS_ENABLED: bool = _bool_env("SOCIALS_ENABLED", True)
    SOCIALS_ASYNC_ONLY: bool = _bool_env("SOCIALS_ASYNC_ONLY", True)
    SOCIALS_HOT_PATH_BLOCKING: bool = _bool_env("SOCIALS_HOT_PATH_BLOCKING", False)
    SOCIALS_TIMEOUT_S: float = _num_env("SOCIALS_TIMEOUT_S", float, 2.0)
    SOCIALS_CACHE_TTL_S: int = _num_env("SOCIALS_CACHE_TTL_S", int, 600)
    SOCIALS_MAX_CONCURRENT: int = _num_env("SOCIALS_MAX_CONCURRENT", int, 4)
    SOCIALS_SUSPICIOUS_ENABLED: bool = _bool_env("SOCIALS_SUSPICIOUS_ENABLED", True)
    SOCIALS_REUSED_LINK_LOOKBACK_DAYS: int = _num_env("SOCIALS_REUSED_LINK_LOOKBACK_DAYS", int, 7)
    SOCIALS_REUSED_LINK_MAX_TOKENS: int = _num_env("SOCIALS_REUSED_LINK_MAX_TOKENS", int, 3)
    GREEN_SNIPER_REQUIRE_SOCIALS: bool = _bool_env("GREEN_SNIPER_REQUIRE_SOCIALS", False)
    GREEN_SNIPER_SOCIALS_BONUS_ENABLED: bool = _bool_env("GREEN_SNIPER_SOCIALS_BONUS_ENABLED", True)
    GREEN_SNIPER_SOCIALS_SCORE_BONUS: float = _num_env("GREEN_SNIPER_SOCIALS_SCORE_BONUS", float, 5.0)
    GREEN_SNIPER_SOCIALS_RISK_PENALTY: float = _num_env("GREEN_SNIPER_SOCIALS_RISK_PENALTY", float, 5.0)
    GREEN_SNIPER_SOCIALS_CAN_INCREASE_SIZE_PAPER: bool = _bool_env("GREEN_SNIPER_SOCIALS_CAN_INCREASE_SIZE_PAPER", True)
    GREEN_SNIPER_SOCIALS_CAN_INCREASE_SIZE_LIVE: bool = _bool_env("GREEN_SNIPER_SOCIALS_CAN_INCREASE_SIZE_LIVE", False)
    GREEN_SNIPER_SOCIALS_CAN_DECREASE_SIZE: bool = _bool_env("GREEN_SNIPER_SOCIALS_CAN_DECREASE_SIZE", True)
    GREEN_SNIPER_SOCIALS_MAX_SIZE_BONUS_TIER: int = _num_env("GREEN_SNIPER_SOCIALS_MAX_SIZE_BONUS_TIER", int, 1)
    GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_BLOCK: bool = _bool_env("GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_BLOCK", False)
    GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_REDUCE_SIZE: bool = _bool_env("GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_REDUCE_SIZE", True)
    SNIPER_EXPERIMENT_ID: str = os.getenv("SNIPER_EXPERIMENT_ID", "green_v1")
    SNIPER_STRATEGY_VERSION: str = os.getenv("SNIPER_STRATEGY_VERSION", "2026-04-green-sniper-v1")
    GREEN_SNIPER_TP_PARTIAL_ENABLED: bool = _bool_env("GREEN_SNIPER_TP_PARTIAL_ENABLED", True)
    GREEN_SNIPER_TP_PARTIAL_TRIGGER_PCT: float = _num_env("GREEN_SNIPER_TP_PARTIAL_TRIGGER_PCT", float, 25.0)
    GREEN_SNIPER_TP_PARTIAL_FRACTION: float = _num_env("GREEN_SNIPER_TP_PARTIAL_FRACTION", float, 0.25)
    GREEN_SNIPER_STEP1_PEAK_PCT: float = _num_env("GREEN_SNIPER_STEP1_PEAK_PCT", float, 60.0)
    GREEN_SNIPER_STEP1_LOCK_FLOOR_PCT: float = _num_env("GREEN_SNIPER_STEP1_LOCK_FLOOR_PCT", float, 20.0)
    GREEN_SNIPER_STEP1_MAX_GIVEBACK_PCT: float = _num_env("GREEN_SNIPER_STEP1_MAX_GIVEBACK_PCT", float, 5.0)
    GREEN_SNIPER_STEP2_PEAK_PCT: float = _num_env("GREEN_SNIPER_STEP2_PEAK_PCT", float, 120.0)
    GREEN_SNIPER_STEP2_LOCK_FLOOR_PCT: float = _num_env("GREEN_SNIPER_STEP2_LOCK_FLOOR_PCT", float, 80.0)
    GREEN_SNIPER_STEP2_MAX_GIVEBACK_PCT: float = _num_env("GREEN_SNIPER_STEP2_MAX_GIVEBACK_PCT", float, 10.0)
    GREEN_SNIPER_STEP3_PEAK_PCT: float = _num_env("GREEN_SNIPER_STEP3_PEAK_PCT", float, 250.0)
    GREEN_SNIPER_STEP3_LOCK_FLOOR_PCT: float = _num_env("GREEN_SNIPER_STEP3_LOCK_FLOOR_PCT", float, 160.0)
    GREEN_SNIPER_STEP3_MAX_GIVEBACK_PCT: float = _num_env("GREEN_SNIPER_STEP3_MAX_GIVEBACK_PCT", float, 20.0)
    GREEN_SNIPER_STEP4_PEAK_PCT: float = _num_env("GREEN_SNIPER_STEP4_PEAK_PCT", float, 700.0)
    GREEN_SNIPER_STEP4_LOCK_FLOOR_PCT: float = _num_env("GREEN_SNIPER_STEP4_LOCK_FLOOR_PCT", float, 420.0)
    GREEN_SNIPER_STEP4_MAX_GIVEBACK_PCT: float = _num_env("GREEN_SNIPER_STEP4_MAX_GIVEBACK_PCT", float, 220.0)
    GREEN_SNIPER_STEP5_PEAK_PCT: float = _num_env("GREEN_SNIPER_STEP5_PEAK_PCT", float, 1500.0)
    GREEN_SNIPER_STEP5_LOCK_FLOOR_PCT: float = _num_env("GREEN_SNIPER_STEP5_LOCK_FLOOR_PCT", float, 900.0)
    GREEN_SNIPER_STEP5_MAX_GIVEBACK_PCT: float = _num_env("GREEN_SNIPER_STEP5_MAX_GIVEBACK_PCT", float, 450.0)
    GREEN_SNIPER_POST_PARTIAL_PROTECTION_ENABLED: bool = _bool_env("GREEN_SNIPER_POST_PARTIAL_PROTECTION_ENABLED", True)
    GREEN_SNIPER_POST_PARTIAL_LOCK_FLOOR_PCT: float = _num_env("GREEN_SNIPER_POST_PARTIAL_LOCK_FLOOR_PCT", float, 20.0)
    GREEN_SNIPER_POST_PARTIAL_MAX_GIVEBACK_PCT: float = _num_env("GREEN_SNIPER_POST_PARTIAL_MAX_GIVEBACK_PCT", float, 5.0)
    GREEN_SNIPER_POST_PARTIAL_MIN_PEAK_PCT: float = _num_env("GREEN_SNIPER_POST_PARTIAL_MIN_PEAK_PCT", float, 35.0)
    GREEN_SNIPER_ADVERSE_TICK_AFTER_S: int = _num_env("GREEN_SNIPER_ADVERSE_TICK_AFTER_S", int, 45)
    GREEN_SNIPER_ADVERSE_TICK_PNL_PCT: float = _num_env("GREEN_SNIPER_ADVERSE_TICK_PNL_PCT", float, -10.0)
    GREEN_SNIPER_NO_PUMP_WINDOW_MIN: float = _num_env("GREEN_SNIPER_NO_PUMP_WINDOW_MIN", float, 2.0)
    GREEN_SNIPER_NO_PUMP_MIN_PEAK_PCT: float = _num_env("GREEN_SNIPER_NO_PUMP_MIN_PEAK_PCT", float, 8.0)
    GREEN_SNIPER_NO_PUMP_MAX_PNL_PCT: float = _num_env("GREEN_SNIPER_NO_PUMP_MAX_PNL_PCT", float, 0.0)
    PUMP_EARLY_METEOR_PRIME_ENABLED: bool = _bool_env("PUMP_EARLY_METEOR_PRIME_ENABLED", False)
    PUMP_EARLY_METEOR_PRIME_MIN_LIQUIDITY_USD: float = _num_env(
        "PUMP_EARLY_METEOR_PRIME_MIN_LIQUIDITY_USD",
        float,
        4_000.0,
    )
    PUMP_EARLY_METEOR_PRIME_MAX_LIQUIDITY_USD: float = _num_env(
        "PUMP_EARLY_METEOR_PRIME_MAX_LIQUIDITY_USD",
        float,
        30_000.0,
    )
    PUMP_EARLY_METEOR_PRIME_MIN_MARKET_CAP_USD: float = _num_env(
        "PUMP_EARLY_METEOR_PRIME_MIN_MARKET_CAP_USD",
        float,
        5_000.0,
    )
    PUMP_EARLY_METEOR_PRIME_MAX_MARKET_CAP_USD: float = _num_env(
        "PUMP_EARLY_METEOR_PRIME_MAX_MARKET_CAP_USD",
        float,
        30_000.0,
    )
    PUMP_EARLY_METEOR_PRIME_MIN_PRICE_PCT_5M: float = _num_env(
        "PUMP_EARLY_METEOR_PRIME_MIN_PRICE_PCT_5M",
        float,
        110.0,
    )
    PUMP_EARLY_METEOR_PRIME_MAX_PRICE_PCT_5M: float = _num_env(
        "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_PCT_5M",
        float,
        300.0,
    )
    PUMP_EARLY_METEOR_PRIME_MIN_TXNS_5M: int = _num_env("PUMP_EARLY_METEOR_PRIME_MIN_TXNS_5M", int, 220)
    PUMP_EARLY_METEOR_PRIME_MIN_SCORE_TOTAL: int = _num_env(
        "PUMP_EARLY_METEOR_PRIME_MIN_SCORE_TOTAL",
        int,
        30,
    )
    PUMP_EARLY_METEOR_PRIME_MIN_AGE_MIN: float = _num_env(
        "PUMP_EARLY_METEOR_PRIME_MIN_AGE_MIN",
        float,
        3.0,
    )
    PUMP_EARLY_METEOR_PRIME_MAX_AGE_MIN: float = _num_env(
        "PUMP_EARLY_METEOR_PRIME_MAX_AGE_MIN",
        float,
        18.0,
    )
    PUMP_EARLY_METEOR_PRIME_MAX_PRICE_IMPACT_PCT: float = _num_env(
        "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_IMPACT_PCT",
        float,
        12.0,
    )
    PUMP_EARLY_METEOR_PRIME_MIN_VOLUME_USD_24H: float = _num_env(
        "PUMP_EARLY_METEOR_PRIME_MIN_VOLUME_USD_24H",
        float,
        8_000.0,
    )
    PUMP_EARLY_BREAKOUT_PROBE_ENABLED: bool = _bool_env("PUMP_EARLY_BREAKOUT_PROBE_ENABLED", True)
    PUMP_EARLY_BREAKOUT_MIN_LIQUIDITY_USD: float = _num_env(
        "PUMP_EARLY_BREAKOUT_MIN_LIQUIDITY_USD",
        float,
        5_000.0,
    )
    PUMP_EARLY_BREAKOUT_MAX_LIQUIDITY_USD: float = _num_env(
        "PUMP_EARLY_BREAKOUT_MAX_LIQUIDITY_USD",
        float,
        30_000.0,
    )
    PUMP_EARLY_BREAKOUT_MIN_MARKET_CAP_USD: float = _num_env(
        "PUMP_EARLY_BREAKOUT_MIN_MARKET_CAP_USD",
        float,
        5_000.0,
    )
    PUMP_EARLY_BREAKOUT_MAX_MARKET_CAP_USD: float = _num_env(
        "PUMP_EARLY_BREAKOUT_MAX_MARKET_CAP_USD",
        float,
        60_000.0,
    )
    PUMP_EARLY_BREAKOUT_MIN_PRICE_PCT_5M: float = _num_env(
        "PUMP_EARLY_BREAKOUT_MIN_PRICE_PCT_5M",
        float,
        25.0,
    )
    PUMP_EARLY_BREAKOUT_MAX_PRICE_PCT_5M: float = _num_env(
        "PUMP_EARLY_BREAKOUT_MAX_PRICE_PCT_5M",
        float,
        120.0,
    )
    PUMP_EARLY_BREAKOUT_MIN_TXNS_5M: int = _num_env("PUMP_EARLY_BREAKOUT_MIN_TXNS_5M", int, 300)
    PUMP_EARLY_BREAKOUT_MIN_VOLUME_USD_24H: float = _num_env(
        "PUMP_EARLY_BREAKOUT_MIN_VOLUME_USD_24H",
        float,
        20_000.0,
    )
    PUMP_EARLY_BREAKOUT_MIN_SCORE_TOTAL: int = _num_env("PUMP_EARLY_BREAKOUT_MIN_SCORE_TOTAL", int, 35)
    PUMP_EARLY_BREAKOUT_MIN_RANK_SCORE: float = _num_env(
        "PUMP_EARLY_BREAKOUT_MIN_RANK_SCORE",
        float,
        50.0,
    )
    PUMP_EARLY_BREAKOUT_MIN_AGE_MIN: float = _num_env("PUMP_EARLY_BREAKOUT_MIN_AGE_MIN", float, 2.0)
    PUMP_EARLY_BREAKOUT_MAX_AGE_MIN: float = _num_env("PUMP_EARLY_BREAKOUT_MAX_AGE_MIN", float, 15.0)
    PUMP_EARLY_BREAKOUT_MAX_PRICE_IMPACT_PCT: float = _num_env(
        "PUMP_EARLY_BREAKOUT_MAX_PRICE_IMPACT_PCT",
        float,
        8.0,
    )
    PUMP_EARLY_BREAKOUT_MAX_OPEN_PAPER: int = _num_env("PUMP_EARLY_BREAKOUT_MAX_OPEN_PAPER", int, 1)
    PUMP_EARLY_BREAKOUT_MAX_OPEN_LIVE_CANARY: int = _num_env(
        "PUMP_EARLY_BREAKOUT_MAX_OPEN_LIVE_CANARY",
        int,
        1,
    )
    PUMP_EARLY_BREAKOUT_HEALTH_ISOLATED: bool = _bool_env("PUMP_EARLY_BREAKOUT_HEALTH_ISOLATED", True)
    PUMPSWAP_PRIME_STRICT_ENABLED: bool = _bool_env("PUMPSWAP_PRIME_STRICT_ENABLED", True)
    PUMPSWAP_PRIME_MIN_TXNS_5M: int = _num_env("PUMPSWAP_PRIME_MIN_TXNS_5M", int, 500)
    PUMPSWAP_PRIME_MIN_LIQUIDITY_USD: float = _num_env("PUMPSWAP_PRIME_MIN_LIQUIDITY_USD", float, 10_000.0)
    PUMPSWAP_PRIME_REQUIRE_REAL_LIQUIDITY: bool = _bool_env("PUMPSWAP_PRIME_REQUIRE_REAL_LIQUIDITY", True)
    PUMPSWAP_PRIME_REQUIRE_ROUTE: bool = _bool_env("PUMPSWAP_PRIME_REQUIRE_ROUTE", True)
    PUMPSWAP_PRIME_MAX_PRICE_IMPACT_PCT: float = _num_env("PUMPSWAP_PRIME_MAX_PRICE_IMPACT_PCT", float, 12.0)
    PUMPSWAP_PRIME_SHADOW_IF_NOT_STRICT: bool = _bool_env("PUMPSWAP_PRIME_SHADOW_IF_NOT_STRICT", True)
    PUMPSWAP_REBOUND_PRIME_ENABLED: bool = _bool_env("PUMPSWAP_REBOUND_PRIME_ENABLED", True)
    PUMPSWAP_REBOUND_PRIME_MAX_PRICE5M: float = _num_env("PUMPSWAP_REBOUND_PRIME_MAX_PRICE5M", float, -25.0)
    PUMPSWAP_REBOUND_PRIME_MIN_TXNS_5M: int = _num_env("PUMPSWAP_REBOUND_PRIME_MIN_TXNS_5M", int, 500)
    PUMPSWAP_REBOUND_PRIME_MIN_LIQUIDITY_USD: float = _num_env(
        "PUMPSWAP_REBOUND_PRIME_MIN_LIQUIDITY_USD",
        float,
        10_000.0,
    )
    PUMPSWAP_REBOUND_PRIME_MIN_MCAP_USD: float = _num_env("PUMPSWAP_REBOUND_PRIME_MIN_MCAP_USD", float, 10_000.0)
    PUMPSWAP_REBOUND_PRIME_MAX_MCAP_USD: float = _num_env("PUMPSWAP_REBOUND_PRIME_MAX_MCAP_USD", float, 50_000.0)
    PUMPSWAP_REBOUND_PRIME_REQUIRE_REAL_LIQUIDITY: bool = _bool_env(
        "PUMPSWAP_REBOUND_PRIME_REQUIRE_REAL_LIQUIDITY",
        True,
    )
    PUMPSWAP_REBOUND_PRIME_REQUIRE_ROUTE: bool = _bool_env("PUMPSWAP_REBOUND_PRIME_REQUIRE_ROUTE", True)
    PUMPSWAP_REBOUND_PRIME_MAX_PRICE_IMPACT_PCT: float = _num_env(
        "PUMPSWAP_REBOUND_PRIME_MAX_PRICE_IMPACT_PCT",
        float,
        12.0,
    )
    PUMPSWAP_REBOUND_PRIME_REQUIRE_CONFIRMATION: bool = _bool_env(
        "PUMPSWAP_REBOUND_PRIME_REQUIRE_CONFIRMATION",
        True,
    )
    PUMPSWAP_REBOUND_CONFIRMATION_MIN_RECOVERY_PCT: float = _num_env(
        "PUMPSWAP_REBOUND_CONFIRMATION_MIN_RECOVERY_PCT",
        float,
        10.0,
    )
    PUMPSWAP_REBOUND_CONFIRMATION_HARD_RECOVERY_PCT: float = _num_env(
        "PUMPSWAP_REBOUND_CONFIRMATION_HARD_RECOVERY_PCT",
        float,
        15.0,
    )
    PUMPSWAP_REBOUND_CONFIRMATION_MIN_PRE_ENTRY_PEAK_PCT: float = _num_env(
        "PUMPSWAP_REBOUND_CONFIRMATION_MIN_PRE_ENTRY_PEAK_PCT",
        float,
        8.0,
    )
    PUMPSWAP_REBOUND_CONFIRMATION_HARD_PRE_ENTRY_PEAK_PCT: float = _num_env(
        "PUMPSWAP_REBOUND_CONFIRMATION_HARD_PRE_ENTRY_PEAK_PCT",
        float,
        10.0,
    )
    SNIPER_RESEARCH_SUBPROFILES_ENABLED: bool = _bool_env("SNIPER_RESEARCH_SUBPROFILES_ENABLED", True)
    SNIPER_RESEARCH_MOMENTUM_IGNITION_ENABLED: bool = _bool_env(
        "SNIPER_RESEARCH_MOMENTUM_IGNITION_ENABLED",
        True,
    )
    SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M: float = _num_env(
        "SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M",
        float,
        100.0,
    )
    SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M: float = _num_env(
        "SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M",
        float,
        150.0,
    )
    SNIPER_RESEARCH_MOMENTUM_MIN_LIQUIDITY_USD: float = _num_env(
        "SNIPER_RESEARCH_MOMENTUM_MIN_LIQUIDITY_USD",
        float,
        15_000.0,
    )
    SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M: int = _num_env(
        "SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M",
        int,
        500,
    )
    SNIPER_RESEARCH_MOMENTUM_MAX_TXNS_5M: int = _num_env(
        "SNIPER_RESEARCH_MOMENTUM_MAX_TXNS_5M",
        int,
        500,
    )
    SNIPER_RESEARCH_MOMENTUM_MIN_MCAP_USD: float = _num_env(
        "SNIPER_RESEARCH_MOMENTUM_MIN_MCAP_USD",
        float,
        15_000.0,
    )
    SNIPER_RESEARCH_MOMENTUM_MAX_MCAP_USD: float = _num_env(
        "SNIPER_RESEARCH_MOMENTUM_MAX_MCAP_USD",
        float,
        70_000.0,
    )
    SNIPER_RESEARCH_MOMENTUM_MAX_TOP10_SHARE_PCT: float = _num_env(
        "SNIPER_RESEARCH_MOMENTUM_MAX_TOP10_SHARE_PCT",
        float,
        40.0,
    )
    SNIPER_RESEARCH_MOMENTUM_ALLOW_TREND_MISSING_IF_STRONG: bool = _bool_env(
        "SNIPER_RESEARCH_MOMENTUM_ALLOW_TREND_MISSING_IF_STRONG",
        True,
    )
    SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_TXNS_5M: int = _num_env(
        "SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_TXNS_5M",
        int,
        1200,
    )
    SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_RANK: float = _num_env(
        "SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_RANK",
        float,
        70.0,
    )
    SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_LIQUIDITY: float = _num_env(
        "SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_LIQUIDITY",
        float,
        25_000.0,
    )
    SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_VOLUME_24H: float = _num_env(
        "SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_VOLUME_24H",
        float,
        100_000.0,
    )
    SNIPER_RESEARCH_MOMENTUM_STRONG_MAX_QUEUE_AGE_MIN: float = _num_env(
        "SNIPER_RESEARCH_MOMENTUM_STRONG_MAX_QUEUE_AGE_MIN",
        float,
        5.0,
    )
    SNIPER_RESEARCH_DEEP_REVERSAL_ENABLED: bool = _bool_env(
        "SNIPER_RESEARCH_DEEP_REVERSAL_ENABLED",
        True,
    )
    SNIPER_RESEARCH_DEEP_REVERSAL_MIN_PRICE5M: float = _num_env(
        "SNIPER_RESEARCH_DEEP_REVERSAL_MIN_PRICE5M",
        float,
        -90.0,
    )
    SNIPER_RESEARCH_DEEP_REVERSAL_MAX_PRICE5M: float = _num_env(
        "SNIPER_RESEARCH_DEEP_REVERSAL_MAX_PRICE5M",
        float,
        -50.0,
    )
    SNIPER_RESEARCH_DEEP_REVERSAL_MIN_TXNS_5M: int = _num_env(
        "SNIPER_RESEARCH_DEEP_REVERSAL_MIN_TXNS_5M",
        int,
        500,
    )
    SNIPER_RESEARCH_DEEP_REVERSAL_MAX_MCAP_USD: float = _num_env(
        "SNIPER_RESEARCH_DEEP_REVERSAL_MAX_MCAP_USD",
        float,
        25_000.0,
    )
    SNIPER_RESEARCH_DEEP_REVERSAL_TAKE_PROFIT_PCT: float = _num_env(
        "SNIPER_RESEARCH_DEEP_REVERSAL_TAKE_PROFIT_PCT",
        float,
        12.0,
    )
    SNIPER_RESEARCH_DEEP_REVERSAL_STOP_LOSS_PCT: float = _num_env(
        "SNIPER_RESEARCH_DEEP_REVERSAL_STOP_LOSS_PCT",
        float,
        4.0,
    )
    SNIPER_RESEARCH_DEEP_REVERSAL_TRAILING_PCT: float = _num_env(
        "SNIPER_RESEARCH_DEEP_REVERSAL_TRAILING_PCT",
        float,
        6.0,
    )
    SNIPER_RESEARCH_DEEP_REVERSAL_TIME_STOP_MIN: float = _num_env(
        "SNIPER_RESEARCH_DEEP_REVERSAL_TIME_STOP_MIN",
        float,
        2.0,
    )
    SNIPER_RESEARCH_DEEP_REVERSAL_TIME_STOP_MAX_PNL_PCT: float = _num_env(
        "SNIPER_RESEARCH_DEEP_REVERSAL_TIME_STOP_MAX_PNL_PCT",
        float,
        5.0,
    )
    SNIPER_RESEARCH_DEEP_REVERSAL_TIME_STOP_MIN_PEAK_PCT: float = _num_env(
        "SNIPER_RESEARCH_DEEP_REVERSAL_TIME_STOP_MIN_PEAK_PCT",
        float,
        8.0,
    )
    PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED: bool = _bool_env("PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED", True)
    PUMP_EARLY_PROFIT_HEALTH_REBASE_CURRENT_GATE: bool = _bool_env(
        "PUMP_EARLY_PROFIT_HEALTH_REBASE_CURRENT_GATE",
        True,
    )
    PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD: float = _num_env(
        "PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD",
        float,
        25_000.0,
    )
    PUMP_EARLY_PROFIT_DEEP_NEG_PRICE5M_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_DEEP_NEG_PRICE5M_PCT",
        float,
        -40.0,
    )
    PUMP_EARLY_PROFIT_DEEP_NEG_MIN_TXNS_5M: int = _num_env(
        "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_TXNS_5M",
        int,
        1_500,
    )
    PUMP_EARLY_PROFIT_DEEP_NEG_MIN_VOLUME_USD_24H: float = _num_env(
        "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_VOLUME_USD_24H",
        float,
        150_000.0,
    )
    PUMP_EARLY_PROFIT_EXTREME_PRICE5M_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_PCT",
        float,
        300.0,
    )
    PUMP_EARLY_PROFIT_EXTREME_PRICE5M_MIN_MCAP_USD: float = _num_env(
        "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_MIN_MCAP_USD",
        float,
        100_000.0,
    )
    PUMP_EARLY_PROFIT_DEAD_VOLUME_MIN_USD_24H: float = _num_env(
        "PUMP_EARLY_PROFIT_DEAD_VOLUME_MIN_USD_24H",
        float,
        15_000.0,
    )
    PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_USD_24H: float = _num_env(
        "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_USD_24H",
        float,
        30_000.0,
    )
    PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_TXNS_5M: int = _num_env(
        "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_TXNS_5M",
        int,
        1_000,
    )
    PUMP_EARLY_PROFIT_HOT_PRICE5M_MIN_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_HOT_PRICE5M_MIN_PCT",
        float,
        100.0,
    )
    PUMP_EARLY_PROFIT_HOT_PRICE5M_MAX_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_HOT_PRICE5M_MAX_PCT",
        float,
        180.0,
    )
    PUMP_EARLY_PROFIT_HOT_MCAP_MIN_USD: float = _num_env(
        "PUMP_EARLY_PROFIT_HOT_MCAP_MIN_USD",
        float,
        50_000.0,
    )
    PUMP_EARLY_PROFIT_HOT_MIN_LIQUIDITY_USD: float = _num_env(
        "PUMP_EARLY_PROFIT_HOT_MIN_LIQUIDITY_USD",
        float,
        20_000.0,
    )
    PUMP_EARLY_PROFIT_HOT_MIN_TXNS_5M: int = _num_env("PUMP_EARLY_PROFIT_HOT_MIN_TXNS_5M", int, 600)
    PUMP_EARLY_PROFIT_HOT_MIN_VOLUME_USD_24H: float = _num_env(
        "PUMP_EARLY_PROFIT_HOT_MIN_VOLUME_USD_24H",
        float,
        50_000.0,
    )
    PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H: float = _num_env(
        "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H",
        float,
        0.0,
    )
    PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_TXNS_5M: int = _num_env(
        "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_TXNS_5M",
        int,
        500,
    )
    PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_PRICE5M_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_PRICE5M_PCT",
        float,
        50.0,
    )
    PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_TXNS_5M: int = _num_env(
        "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_TXNS_5M",
        int,
        350,
    )
    PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_VOLUME_USD_24H: float = _num_env(
        "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_VOLUME_USD_24H",
        float,
        100_000.0,
    )
    PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT",
        float,
        40.0,
    )
    PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT",
        float,
        50.0,
    )
    PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD: float = _num_env(
        "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD",
        float,
        100_000.0,
    )
    PUMP_EARLY_PROFIT_PNL_GUARD_ENABLED: bool = _bool_env("PUMP_EARLY_PROFIT_PNL_GUARD_ENABLED", True)
    PUMP_EARLY_PROFIT_PNL_GUARD_JACKPOT_PRICE5M_MIN: float = _num_env(
        "PUMP_EARLY_PROFIT_PNL_GUARD_JACKPOT_PRICE5M_MIN",
        float,
        180.0,
    )
    PUMP_EARLY_PROFIT_PNL_GUARD_50K_100K_WEAK_PRICE5M_MAX: float = _num_env(
        "PUMP_EARLY_PROFIT_PNL_GUARD_50K_100K_WEAK_PRICE5M_MAX",
        float,
        25.0,
    )
    PUMP_EARLY_PROFIT_PNL_GUARD_50K_100K_WEAK_MIN_TXNS_5M: int = _num_env(
        "PUMP_EARLY_PROFIT_PNL_GUARD_50K_100K_WEAK_MIN_TXNS_5M",
        int,
        700,
    )
    PUMP_EARLY_PROFIT_PNL_GUARD_LOCAL_TOP_MIN_MCAP_USD: float = _num_env(
        "PUMP_EARLY_PROFIT_PNL_GUARD_LOCAL_TOP_MIN_MCAP_USD",
        float,
        25_000.0,
    )
    PUMP_EARLY_PROFIT_PNL_GUARD_MID_MOMENTUM_MIN_MCAP_USD: float = _num_env(
        "PUMP_EARLY_PROFIT_PNL_GUARD_MID_MOMENTUM_MIN_MCAP_USD",
        float,
        50_000.0,
    )
    PUMP_EARLY_PROFIT_MAX_OPEN_PAPER: int = _num_env("PUMP_EARLY_PROFIT_MAX_OPEN_PAPER", int, 2)
    PUMP_EARLY_PROFIT_MAX_OPEN_LIVE_CANARY: int = _num_env(
        "PUMP_EARLY_PROFIT_MAX_OPEN_LIVE_CANARY",
        int,
        1,
    )
    PUMP_EARLY_PROFIT_RUNNER_BROAD_LOCK_FLOOR_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_BROAD_LOCK_FLOOR_PCT",
        float,
        20.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_BROAD_PARTIAL_FRACTION: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_BROAD_PARTIAL_FRACTION",
        float,
        0.80,
    )
    PUMP_EARLY_PROFIT_RUNNER_BROAD_MAX_GIVEBACK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_BROAD_MAX_GIVEBACK_PCT",
        float,
        5.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_LOCK_FLOOR_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_LOCK_FLOOR_PCT",
        float,
        25.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_PRIME_PARTIAL_FRACTION: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_PRIME_PARTIAL_FRACTION",
        float,
        0.65,
    )
    PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_MAX_GIVEBACK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_MAX_GIVEBACK_PCT",
        float,
        10.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_PEAK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_PEAK_PCT",
        float,
        80.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_LOCK_FLOOR_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_LOCK_FLOOR_PCT",
        float,
        45.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_MAX_GIVEBACK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_MAX_GIVEBACK_PCT",
        float,
        15.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_LOCK_FLOOR_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_LOCK_FLOOR_PCT",
        float,
        25.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_METEOR_PARTIAL_FRACTION: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_METEOR_PARTIAL_FRACTION",
        float,
        0.50,
    )
    PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_MAX_GIVEBACK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_MAX_GIVEBACK_PCT",
        float,
        15.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_PEAK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_PEAK_PCT",
        float,
        100.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_LOCK_FLOOR_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_LOCK_FLOOR_PCT",
        float,
        70.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_MAX_GIVEBACK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_MAX_GIVEBACK_PCT",
        float,
        20.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP2_PEAK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP2_PEAK_PCT",
        float,
        250.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP2_LOCK_FLOOR_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP2_LOCK_FLOOR_PCT",
        float,
        120.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_ENABLED: bool = _bool_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_ENABLED",
        True,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_LIQUIDITY_USD: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_LIQUIDITY_USD",
        float,
        10_000.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_MCAP_USD: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_MCAP_USD",
        float,
        30_000.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MAX_MCAP_USD: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MAX_MCAP_USD",
        float,
        100_000.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_PRICE5M_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_PRICE5M_PCT",
        float,
        25.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MAX_PRICE5M_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MAX_PRICE5M_PCT",
        float,
        100.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_TXNS_5M: int = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_TXNS_5M",
        int,
        300,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_RANK_SCORE: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_RANK_SCORE",
        float,
        58.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_PARTIAL_FRACTION: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_PARTIAL_FRACTION",
        float,
        0.35,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_BASE_LOCK_FLOOR_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_BASE_LOCK_FLOOR_PCT",
        float,
        35.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_BASE_MAX_GIVEBACK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_BASE_MAX_GIVEBACK_PCT",
        float,
        12.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_PEAK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_PEAK_PCT",
        float,
        100.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_LOCK_FLOOR_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_LOCK_FLOOR_PCT",
        float,
        80.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_MAX_GIVEBACK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_MAX_GIVEBACK_PCT",
        float,
        18.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_PEAK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_PEAK_PCT",
        float,
        300.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_LOCK_FLOOR_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_LOCK_FLOOR_PCT",
        float,
        180.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_MAX_GIVEBACK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_MAX_GIVEBACK_PCT",
        float,
        25.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_PEAK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_PEAK_PCT",
        float,
        500.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_LOCK_FLOOR_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_LOCK_FLOOR_PCT",
        float,
        320.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_MAX_GIVEBACK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_MAX_GIVEBACK_PCT",
        float,
        120.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_PEAK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_PEAK_PCT",
        float,
        1000.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_LOCK_FLOOR_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_LOCK_FLOOR_PCT",
        float,
        650.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_MAX_GIVEBACK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_MAX_GIVEBACK_PCT",
        float,
        220.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP1_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP1_PCT",
        float,
        100.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP1_FRACTION: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP1_FRACTION",
        float,
        0.20,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP2_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP2_PCT",
        float,
        300.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP2_FRACTION: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP2_FRACTION",
        float,
        0.20,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP3_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP3_PCT",
        float,
        500.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP3_FRACTION: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP3_FRACTION",
        float,
        0.15,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP4_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP4_PCT",
        float,
        1000.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP4_FRACTION: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP4_FRACTION",
        float,
        0.15,
    )
    PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MOONBAG_FRACTION: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MOONBAG_FRACTION",
        float,
        0.30,
    )
    GREEN_SNIPER_MOONSHOT_TP1_PCT: float = _num_env("GREEN_SNIPER_MOONSHOT_TP1_PCT", float, 25.0)
    GREEN_SNIPER_MOONSHOT_TP1_FRACTION: float = _num_env("GREEN_SNIPER_MOONSHOT_TP1_FRACTION", float, 0.15)
    GREEN_SNIPER_MOONSHOT_TP2_PCT: float = _num_env("GREEN_SNIPER_MOONSHOT_TP2_PCT", float, 100.0)
    GREEN_SNIPER_MOONSHOT_TP2_FRACTION: float = _num_env("GREEN_SNIPER_MOONSHOT_TP2_FRACTION", float, 0.15)
    GREEN_SNIPER_MOONSHOT_TP3_PCT: float = _num_env("GREEN_SNIPER_MOONSHOT_TP3_PCT", float, 300.0)
    GREEN_SNIPER_MOONSHOT_TP3_FRACTION: float = _num_env("GREEN_SNIPER_MOONSHOT_TP3_FRACTION", float, 0.15)
    GREEN_SNIPER_MOONSHOT_TP4_PCT: float = _num_env("GREEN_SNIPER_MOONSHOT_TP4_PCT", float, 700.0)
    GREEN_SNIPER_MOONSHOT_TP4_FRACTION: float = _num_env("GREEN_SNIPER_MOONSHOT_TP4_FRACTION", float, 0.15)
    GREEN_SNIPER_MOONSHOT_MOONBAG_FRACTION: float = _num_env(
        "GREEN_SNIPER_MOONSHOT_MOONBAG_FRACTION",
        float,
        0.40,
    )
    MOONSHOT_MICRO_LOTTERY_ENABLED: bool = _bool_env("MOONSHOT_MICRO_LOTTERY_ENABLED", True)
    MOONSHOT_MICRO_LOTTERY_PAPER_ENABLED: bool = _bool_env("MOONSHOT_MICRO_LOTTERY_PAPER_ENABLED", True)
    MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED: bool = _bool_env("MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED", False)
    MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL: float = _num_env("MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL", float, 0.002)
    MOONSHOT_MICRO_LOTTERY_MAX_OPEN: int = _num_env("MOONSHOT_MICRO_LOTTERY_MAX_OPEN", int, 1)
    MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS: int = _num_env("MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS", int, 3)
    MOONSHOT_MICRO_LOTTERY_MAX_AGE_MIN: float = _num_env("MOONSHOT_MICRO_LOTTERY_MAX_AGE_MIN", float, 6.0)
    MOONSHOT_MICRO_LOTTERY_MIN_TXNS_5M: int = _num_env("MOONSHOT_MICRO_LOTTERY_MIN_TXNS_5M", int, 80)
    MOONSHOT_MICRO_LOTTERY_MAX_MCAP_USD: float = _num_env("MOONSHOT_MICRO_LOTTERY_MAX_MCAP_USD", float, 150_000.0)
    MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M: float = _num_env("MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M", float, 500.0)
    MOONSHOT_MICRO_LOTTERY_EXTREME_MIN_TXNS_5M: int = _num_env(
        "MOONSHOT_MICRO_LOTTERY_EXTREME_MIN_TXNS_5M",
        int,
        300,
    )
    MOONSHOT_MICRO_LOTTERY_TP1_PCT: float = _num_env("MOONSHOT_MICRO_LOTTERY_TP1_PCT", float, 50.0)
    MOONSHOT_MICRO_LOTTERY_TP1_FRACTION: float = _num_env("MOONSHOT_MICRO_LOTTERY_TP1_FRACTION", float, 0.40)
    MOONSHOT_MICRO_LOTTERY_TP2_PCT: float = _num_env("MOONSHOT_MICRO_LOTTERY_TP2_PCT", float, 100.0)
    MOONSHOT_MICRO_LOTTERY_TP2_FRACTION: float = _num_env("MOONSHOT_MICRO_LOTTERY_TP2_FRACTION", float, 0.25)
    MOONSHOT_MICRO_LOTTERY_TP3_PCT: float = _num_env("MOONSHOT_MICRO_LOTTERY_TP3_PCT", float, 300.0)
    MOONSHOT_MICRO_LOTTERY_TP3_FRACTION: float = _num_env("MOONSHOT_MICRO_LOTTERY_TP3_FRACTION", float, 0.20)
    MOONSHOT_MICRO_LOTTERY_TP4_PCT: float = _num_env("MOONSHOT_MICRO_LOTTERY_TP4_PCT", float, 700.0)
    MOONSHOT_MICRO_LOTTERY_TP4_FRACTION: float = _num_env("MOONSHOT_MICRO_LOTTERY_TP4_FRACTION", float, 0.10)
    MOONSHOT_MICRO_LOTTERY_MOONBAG_FRACTION: float = _num_env(
        "MOONSHOT_MICRO_LOTTERY_MOONBAG_FRACTION",
        float,
        0.05,
    )
    MOONSHOT_MICRO_LOTTERY_TIME_STOP_MIN: float = _num_env("MOONSHOT_MICRO_LOTTERY_TIME_STOP_MIN", float, 2.0)
    MOONSHOT_MICRO_LOTTERY_TIME_STOP_MAX_PNL_PCT: float = _num_env(
        "MOONSHOT_MICRO_LOTTERY_TIME_STOP_MAX_PNL_PCT",
        float,
        10.0,
    )
    MOONSHOT_MICRO_LOTTERY_HARD_STOP_PCT: float = _num_env("MOONSHOT_MICRO_LOTTERY_HARD_STOP_PCT", float, 20.0)
    MOONSHOT_MICRO_LOTTERY_NO_EXPANSION_EXIT_S: int = _num_env(
        "MOONSHOT_MICRO_LOTTERY_NO_EXPANSION_EXIT_S",
        int,
        90,
    )
    PUMP_EARLY_PROFIT_RUNNER_METEOR_MOMENTUM_PRICE5M_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_METEOR_MOMENTUM_PRICE5M_PCT",
        float,
        180.0,
    )
    PUMP_EARLY_PROFIT_RUNNER_METEOR_MOMENTUM_MIN_TXNS_5M: int = _num_env(
        "PUMP_EARLY_PROFIT_RUNNER_METEOR_MOMENTUM_MIN_TXNS_5M",
        int,
        600,
    )
    PUMP_EARLY_RESEARCH_ALLOW_PROXY: bool = _bool_env("PUMP_EARLY_RESEARCH_ALLOW_PROXY", True)
    PAPER_PNL_STRICT_HEALTH: bool = _bool_env("PAPER_PNL_STRICT_HEALTH", True)
    PUMP_EARLY_PROFIT_ADVERSE_TICK_AFTER_S: int = _num_env(
        "PUMP_EARLY_PROFIT_ADVERSE_TICK_AFTER_S",
        int,
        75,
    )
    PUMP_EARLY_PROFIT_ADVERSE_TICK_PNL_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_ADVERSE_TICK_PNL_PCT",
        float,
        -8.0,
    )
    PUMP_EARLY_PROFIT_NO_PUMP_WINDOW_MIN: float = _num_env(
        "PUMP_EARLY_PROFIT_NO_PUMP_WINDOW_MIN",
        float,
        3.0,
    )
    PUMP_EARLY_PROFIT_NO_PUMP_MIN_PEAK_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_NO_PUMP_MIN_PEAK_PCT",
        float,
        2.0,
    )
    PUMP_EARLY_PROFIT_NO_PUMP_MAX_PNL_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_NO_PUMP_MAX_PNL_PCT",
        float,
        0.0,
    )
    LIVE_RANK_SCORE_FALLBACK_MIN: float = _num_env(
        "LIVE_RANK_SCORE_FALLBACK_MIN",
        float,
        12.5,
    )
    LIVE_RANK_SCORE_MIN_SELECTED_ROWS: int = _num_env(
        "LIVE_RANK_SCORE_MIN_SELECTED_ROWS",
        int,
        20,
    )
    LIVE_RANK_SCORE_MIN_AVG_PNL_PCT: float = _num_env(
        "LIVE_RANK_SCORE_MIN_AVG_PNL_PCT",
        float,
        3.0,
    )
    REGIME_HEALTH_WINDOW_TRADES: int = _num_env("REGIME_HEALTH_WINDOW_TRADES", int, 20)
    REGIME_HEALTH_WINDOW_EVENTS: int = _num_env("REGIME_HEALTH_WINDOW_EVENTS", int, 40)
    REGIME_HEALTH_MIN_TRADES: int = _num_env("REGIME_HEALTH_MIN_TRADES", int, 6)
    REGIME_HEALTH_DISABLE_EXPECTANCY_PCT: float = _num_env(
        "REGIME_HEALTH_DISABLE_EXPECTANCY_PCT",
        float,
        -5.0,
    )
    REGIME_HEALTH_RECOVERY_EXPECTANCY_PCT: float = _num_env(
        "REGIME_HEALTH_RECOVERY_EXPECTANCY_PCT",
        float,
        1.0,
    )
    REGIME_HEALTH_MAX_CONSECUTIVE_LOSSES: int = _num_env(
        "REGIME_HEALTH_MAX_CONSECUTIVE_LOSSES",
        int,
        4,
    )
    REGIME_HEALTH_MIN_EXEC_SUCCESS_RATE: float = _num_env(
        "REGIME_HEALTH_MIN_EXEC_SUCCESS_RATE",
        float,
        0.70,
    )
    REGIME_HEALTH_MIN_PRICE_COVERAGE_RATE: float = _num_env(
        "REGIME_HEALTH_MIN_PRICE_COVERAGE_RATE",
        float,
        0.70,
    )
    REGIME_HEALTH_COOLDOWN_MIN: int = _num_env("REGIME_HEALTH_COOLDOWN_MIN", int, 120)
    REGIME_HEALTH_DISABLE_ACTION: str = (
        (os.getenv("REGIME_HEALTH_DISABLE_ACTION", "shadow") or "shadow").strip().lower()
    )
    REGIME_HEALTH_COOLDOWN_MAX_SIZE_MULTIPLIER: float = _num_env(
        "REGIME_HEALTH_COOLDOWN_MAX_SIZE_MULTIPLIER",
        float,
        0.10,
    )
    STRATEGY_SCORECARD_OVERRIDE_ENABLED: bool = _bool_env("STRATEGY_SCORECARD_OVERRIDE_ENABLED", True)
    STRATEGY_SCORECARD_MIN_OUTCOMES: int = _num_env("STRATEGY_SCORECARD_MIN_OUTCOMES", int, 12)
    STRATEGY_SCORECARD_MAX_AGE_MIN: float = _num_env("STRATEGY_SCORECARD_MAX_AGE_MIN", float, 240.0)
    STRATEGY_SCORECARD_DEMOTE_MAX_AVG_PNL_PCT: float = _num_env(
        "STRATEGY_SCORECARD_DEMOTE_MAX_AVG_PNL_PCT",
        float,
        -1.0,
    )
    STRATEGY_SCORECARD_PROMOTE_DEX_MATURE_ENABLED: bool = _bool_env(
        "STRATEGY_SCORECARD_PROMOTE_DEX_MATURE_ENABLED",
        True,
    )
    STRATEGY_SCORECARD_PROMOTE_MIN_AVG_PNL_PCT: float = _num_env(
        "STRATEGY_SCORECARD_PROMOTE_MIN_AVG_PNL_PCT",
        float,
        5.0,
    )
    STRATEGY_SCORECARD_PROMOTE_MIN_WIN_RATE_PCT: float = _num_env(
        "STRATEGY_SCORECARD_PROMOTE_MIN_WIN_RATE_PCT",
        float,
        50.0,
    )
    PUMP_EARLY_RECOVERY_MIN_WIN_RATE_PCT: float = _num_env(
        "PUMP_EARLY_RECOVERY_MIN_WIN_RATE_PCT",
        float,
        42.0,
    )
    PUMP_EARLY_RECOVERY_RECENT_OVERRIDE_ENABLED: bool = _bool_env(
        "PUMP_EARLY_RECOVERY_RECENT_OVERRIDE_ENABLED",
        True,
    )
    PUMP_EARLY_RECOVERY_RECENT_TRADES: int = _num_env("PUMP_EARLY_RECOVERY_RECENT_TRADES", int, 6)
    PUMP_EARLY_RECOVERY_RECENT_MIN_AVG_PNL_PCT: float = _num_env(
        "PUMP_EARLY_RECOVERY_RECENT_MIN_AVG_PNL_PCT",
        float,
        5.0,
    )
    PUMP_EARLY_RECOVERY_RECENT_MIN_WIN_RATE_PCT: float = _num_env(
        "PUMP_EARLY_RECOVERY_RECENT_MIN_WIN_RATE_PCT",
        float,
        66.0,
    )
    PUMP_EARLY_RECOVERY_RECENT_IGNORE_OLD_LIQ_CRUSH: bool = _bool_env(
        "PUMP_EARLY_RECOVERY_RECENT_IGNORE_OLD_LIQ_CRUSH",
        True,
    )
    PUMP_EARLY_RECOVERY_DEMOTE_MIN_TRADES: int = _num_env(
        "PUMP_EARLY_RECOVERY_DEMOTE_MIN_TRADES",
        int,
        3,
    )
    PUMP_EARLY_PROFIT_RECOVERY_RECENT_TRADES: int = _num_env(
        "PUMP_EARLY_PROFIT_RECOVERY_RECENT_TRADES",
        int,
        8,
    )
    PUMP_EARLY_PROFIT_RECOVERY_RECENT_MIN_AVG_PNL_PCT: float = _num_env(
        "PUMP_EARLY_PROFIT_RECOVERY_RECENT_MIN_AVG_PNL_PCT",
        float,
        5.0,
    )
    PUMP_EARLY_PROFIT_RECOVERY_RECENT_MAX_CONSECUTIVE_LOSSES: int = _num_env(
        "PUMP_EARLY_PROFIT_RECOVERY_RECENT_MAX_CONSECUTIVE_LOSSES",
        int,
        2,
    )
    PUMP_EARLY_SUBLANE_HEALTH_ENABLED: bool = _bool_env("PUMP_EARLY_SUBLANE_HEALTH_ENABLED", True)
    PUMP_EARLY_SUBLANE_HEALTH_WINDOW_TRADES: int = _num_env(
        "PUMP_EARLY_SUBLANE_HEALTH_WINDOW_TRADES",
        int,
        40,
    )
    PUMP_EARLY_SUBLANE_HEALTH_MIN_TRADES: int = _num_env(
        "PUMP_EARLY_SUBLANE_HEALTH_MIN_TRADES",
        int,
        8,
    )
    PUMP_EARLY_SUBLANE_HEALTH_MAX_AVG_PNL_PCT: float = _num_env(
        "PUMP_EARLY_SUBLANE_HEALTH_MAX_AVG_PNL_PCT",
        float,
        -10.0,
    )
    PUMP_EARLY_SUBLANE_HEALTH_MAX_SEVERE_EXITS: int = _num_env(
        "PUMP_EARLY_SUBLANE_HEALTH_MAX_SEVERE_EXITS",
        int,
        4,
    )
    PUMP_EARLY_SUBLANE_HEALTH_MAX_LIQ_CRUSH_EXITS: int = _num_env(
        "PUMP_EARLY_SUBLANE_HEALTH_MAX_LIQ_CRUSH_EXITS",
        int,
        2,
    )
    PUMP_EARLY_SUBLANE_HEALTH_MIN_CANARY_TRADES: int = _num_env(
        "PUMP_EARLY_SUBLANE_HEALTH_MIN_CANARY_TRADES",
        int,
        3,
    )
    PUMP_EARLY_SUBLANE_HEALTH_MAX_CANARY_AVG_PNL_PCT: float = _num_env(
        "PUMP_EARLY_SUBLANE_HEALTH_MAX_CANARY_AVG_PNL_PCT",
        float,
        -35.0,
    )
    PUMP_EARLY_SUBLANE_HEALTH_MAX_CANARY_SEVERE_EXITS: int = _num_env(
        "PUMP_EARLY_SUBLANE_HEALTH_MAX_CANARY_SEVERE_EXITS",
        int,
        3,
    )
    PUMP_EARLY_SUBLANE_HEALTH_MAX_CANARY_LIQ_CRUSH_EXITS: int = _num_env(
        "PUMP_EARLY_SUBLANE_HEALTH_MAX_CANARY_LIQ_CRUSH_EXITS",
        int,
        2,
    )
    REGIME_RECOVERY_MAX_SIZE_MULTIPLIER: float = _num_env(
        "REGIME_RECOVERY_MAX_SIZE_MULTIPLIER",
        float,
        0.10,
    )
    PUMP_EARLY_RECOVERY_MAX_SIZE_MULTIPLIER: float = _num_env(
        "PUMP_EARLY_RECOVERY_MAX_SIZE_MULTIPLIER",
        float,
        0.10,
    )
    DEX_MATURE_RECOVERY_MAX_SIZE_MULTIPLIER: float = _num_env(
        "DEX_MATURE_RECOVERY_MAX_SIZE_MULTIPLIER",
        float,
        0.10,
    )
    REVIVAL_RECOVERY_MAX_SIZE_MULTIPLIER: float = _num_env(
        "REVIVAL_RECOVERY_MAX_SIZE_MULTIPLIER",
        float,
        0.10,
    )

    # ------- control horario (.env moderno) ------------------------
    TRADING_HOURS: str = os.getenv("TRADING_HOURS", "")                 # ej. "0-2" (local)
    TRADING_HOURS_EXTRA: str = os.getenv("TRADING_HOURS_EXTRA", "")     # ej. "9-10"
    USE_EXTRA_HOURS: bool = _bool_env("USE_EXTRA_HOURS", False)
    LOCAL_TZ_NAME: str = os.getenv("LOCAL_TZ", "Europe/Madrid")
    BLOCK_HOURS: str = os.getenv("BLOCK_HOURS", "")                     # ej. "3,12,17-19"

    # ------- trading windows (legacy, por compatibilidad) ----------
    TRADING_WINDOWS: str = os.getenv("TRADING_WINDOWS", "13-16")
    TRADING_STRICT: bool = _bool_env("TRADING_STRICT", True)

    # ------- compra / requisitos -----------------------------------
    REQUIRE_JUPITER_FOR_BUY: bool = _bool_env("REQUIRE_JUPITER_FOR_BUY", True)
    DEX_WHITELIST: Tuple[str, ...] = _csv_tuple(os.getenv("DEX_WHITELIST", "raydium,orca,meteora"))
    REQUIRE_POOL_INITIALIZED: bool = _bool_env("REQUIRE_POOL_INITIALIZED", True)
    BUY_RATE_LIMIT_N: int = _num_env("BUY_RATE_LIMIT_N", int, 3)
    BUY_RATE_LIMIT_WINDOW_S: int = _num_env("BUY_RATE_LIMIT_WINDOW_S", int, 120)

    # ------- monitor / shadow-sim ----------------------------------
    FORCE_JUP_IN_MONITOR: bool = _bool_env("FORCE_JUP_IN_MONITOR", False)
    REAL_SHADOW_SIM: bool = _bool_env("REAL_SHADOW_SIM", False)
    RESEARCH_SHADOW_USE_GECKO: bool = _bool_env("RESEARCH_SHADOW_USE_GECKO", False)
    PUMPFUN_PRICE_USE_GECKO: bool = _bool_env("PUMPFUN_PRICE_USE_GECKO", False)

    # ------- riesgo / exits ----------------------------------------
    TAKE_PROFIT_PCT: float = _TAKE_PROFIT_PCT_VALUE
    STOP_LOSS_PCT: float = _num_env("STOP_LOSS_PCT", float, 20.0)
    TRAILING_PCT: float = _num_env("TRAILING_PCT", float, 30.0)
    MAX_HOLDING_H: int = _num_env("MAX_HOLDING_H", int, 24)
    MAX_HARD_HOLD_H: int = _num_env("MAX_HARD_HOLD_H", int, 4)

    # Salidas mejoradas
    EARLY_DROP_KILL_PCT: float = _num_env_multi(
        ["EARLY_DROP_KILL_PCT", "KILL_EARLY_DROP_PCT", "EARLY_DROP_PCT"],
        float,
        12.0,
    )  # %
    EARLY_DROP_WINDOW_MIN: int = _num_env_multi(
        ["EARLY_DROP_WINDOW_MIN", "EARLY_WINDOW_MIN"],
        int,
        7,
    )  # min
    LIQ_CRUSH_DROP_PCT: float = _num_env("LIQ_CRUSH_DROP_PCT", float, 35.0)        # %
    LIQ_CRUSH_WINDOW_MIN: int = _num_env("LIQ_CRUSH_WINDOW_MIN", int, 10)         # min
    LIQ_CRUSH_ABS_FRACT: float = _num_env("LIQ_CRUSH_ABS_FRACT", float, 0.60)
    KILL_LIQ_FRACTION: float = _num_env("KILL_LIQ_FRACTION", float, 0.70)
    NO_EXPANSION_MAX_PCT: float = _num_env("NO_EXPANSION_MAX_PCT", float, 0.0)
    TP_PARTIAL_ENABLED: bool = _bool_env("TP_PARTIAL_ENABLED", True)
    TP_PARTIAL_FRACTION: float = _num_env("TP_PARTIAL_FRACTION", float, 0.80)
    TP_PARTIAL_MIN_REMAIN_LAMPORTS: int = _num_env("TP_PARTIAL_MIN_REMAIN_LAMPORTS", int, 1)
    TP_PARTIAL_TRIGGER_PCT: float = _num_env("TP_PARTIAL_TRIGGER_PCT", float, 6.0)
    POST_PARTIAL_STOP_PCT: float = _num_env("POST_PARTIAL_STOP_PCT", float, 0.0)
    POST_PARTIAL_TRAILING_PCT: float = _num_env("POST_PARTIAL_TRAILING_PCT", float, 0.0)
    POST_PARTIAL_PROTECTION_ENABLED: bool = _bool_env("POST_PARTIAL_PROTECTION_ENABLED", True)
    POST_PARTIAL_PROTECTION_PAPER_ENABLED: bool = _bool_env("POST_PARTIAL_PROTECTION_PAPER_ENABLED", True)
    POST_PARTIAL_PROTECTION_LIVE_ENABLED: bool = _bool_env("POST_PARTIAL_PROTECTION_LIVE_ENABLED", False)
    POST_PARTIAL_PROTECTION_EXECUTION_ENABLED: bool = _bool_env(
        "POST_PARTIAL_PROTECTION_EXECUTION_ENABLED",
        True,
    )
    POST_PARTIAL_LOCK_FLOOR_ENABLED: bool = _bool_env("POST_PARTIAL_LOCK_FLOOR_ENABLED", True)
    POST_PARTIAL_LOCK_FLOOR_PCT: float = _num_env("POST_PARTIAL_LOCK_FLOOR_PCT", float, 20.0)
    POST_PARTIAL_MAX_GIVEBACK_PCT: float = _num_env("POST_PARTIAL_MAX_GIVEBACK_PCT", float, 5.0)
    POST_PARTIAL_MIN_PEAK_PCT: float = _num_env("POST_PARTIAL_MIN_PEAK_PCT", float, 35.0)
    POST_PARTIAL_EXPERIMENT_ENABLED: bool = _bool_env("POST_PARTIAL_EXPERIMENT_ENABLED", True)
    POST_PARTIAL_EXPERIMENT_SHADOW_ONLY: bool = _bool_env("POST_PARTIAL_EXPERIMENT_SHADOW_ONLY", False)
    POST_PARTIAL_EXPERIMENT_MODE: str = (
        (os.getenv("POST_PARTIAL_EXPERIMENT_MODE", "paper_shadow") or "paper_shadow").strip().lower()
    )
    POST_PARTIAL_EXPERIMENT_REGIME: str = (
        (os.getenv("POST_PARTIAL_EXPERIMENT_REGIME", "pump_early") or "pump_early").strip().lower()
    )
    POST_PARTIAL_EXPERIMENT_LOCK_FLOOR_PCT: float = _num_env(
        "POST_PARTIAL_EXPERIMENT_LOCK_FLOOR_PCT",
        float,
        20.0,
    )
    POST_PARTIAL_EXPERIMENT_MAX_GIVEBACK_PCT: float = _num_env(
        "POST_PARTIAL_EXPERIMENT_MAX_GIVEBACK_PCT",
        float,
        5.0,
    )
    POST_PARTIAL_EXPERIMENT_MIN_NEW_CLOSES: int = _num_env(
        "POST_PARTIAL_EXPERIMENT_MIN_NEW_CLOSES",
        int,
        50,
    )
    POST_PARTIAL_EXPERIMENT_LOCKED_ML_THRESHOLD: float = _num_env(
        "POST_PARTIAL_EXPERIMENT_LOCKED_ML_THRESHOLD",
        float,
        0.3972866423002348,
    )
    BIRD_RUNNER_MULTI_PARTIAL_ENABLED: bool = _bool_env("BIRD_RUNNER_MULTI_PARTIAL_ENABLED", True)
    BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED: bool = _bool_env("BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED", True)
    BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED: bool = _bool_env("BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED", False)
    BIRD_TP1_PCT: float = _num_env("BIRD_TP1_PCT", float, 25.0)
    BIRD_TP1_FRACTION: float = _num_env("BIRD_TP1_FRACTION", float, 0.25)
    BIRD_TP2_PCT: float = _num_env("BIRD_TP2_PCT", float, 50.0)
    BIRD_TP2_FRACTION: float = _num_env("BIRD_TP2_FRACTION", float, 0.25)
    BIRD_TP3_PCT: float = _num_env("BIRD_TP3_PCT", float, 100.0)
    BIRD_TP3_FRACTION: float = _num_env("BIRD_TP3_FRACTION", float, 0.20)
    BIRD_TP4_PCT: float = _num_env("BIRD_TP4_PCT", float, 300.0)
    BIRD_TP4_FRACTION: float = _num_env("BIRD_TP4_FRACTION", float, 0.15)
    BIRD_TP5_PCT: float = _num_env("BIRD_TP5_PCT", float, 700.0)
    BIRD_TP5_FRACTION: float = _num_env("BIRD_TP5_FRACTION", float, 0.07)
    BIRD_TP6_PCT: float = _num_env("BIRD_TP6_PCT", float, 1000.0)
    BIRD_TP6_FRACTION: float = _num_env("BIRD_TP6_FRACTION", float, 0.05)
    BIRD_MOONBAG_FRACTION: float = _num_env("BIRD_MOONBAG_FRACTION", float, 0.03)
    DYNAMIC_RUNNER_FLOOR_ENABLED: bool = _bool_env("DYNAMIC_RUNNER_FLOOR_ENABLED", True)
    RUNNER_FLOOR_PEAK_100: float = _num_env("RUNNER_FLOOR_PEAK_100", float, 70.0)
    RUNNER_FLOOR_PEAK_300: float = _num_env("RUNNER_FLOOR_PEAK_300", float, 200.0)
    RUNNER_FLOOR_PEAK_700: float = _num_env("RUNNER_FLOOR_PEAK_700", float, 450.0)
    RUNNER_FLOOR_PEAK_1000: float = _num_env("RUNNER_FLOOR_PEAK_1000", float, 700.0)
    RUNNER_FLOOR_PEAK_2000: float = _num_env("RUNNER_FLOOR_PEAK_2000", float, 1200.0)
    RUNNER_GIVEBACK_EMERGENCY_ENABLED: bool = _bool_env("RUNNER_GIVEBACK_EMERGENCY_ENABLED", True)
    RUNNER_GIVEBACK_EMERGENCY_PAPER_ENABLED: bool = _bool_env("RUNNER_GIVEBACK_EMERGENCY_PAPER_ENABLED", True)
    RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED: bool = _bool_env("RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED", False)
    RUNNER_GIVEBACK_PEAK_100_MAX_GIVEBACK: float = _num_env(
        "RUNNER_GIVEBACK_PEAK_100_MAX_GIVEBACK",
        float,
        25.0,
    )
    RUNNER_GIVEBACK_PEAK_300_MAX_GIVEBACK: float = _num_env(
        "RUNNER_GIVEBACK_PEAK_300_MAX_GIVEBACK",
        float,
        60.0,
    )
    RUNNER_GIVEBACK_PEAK_700_MAX_GIVEBACK: float = _num_env(
        "RUNNER_GIVEBACK_PEAK_700_MAX_GIVEBACK",
        float,
        120.0,
    )
    RUNNER_GIVEBACK_PEAK_1000_MAX_GIVEBACK: float = _num_env(
        "RUNNER_GIVEBACK_PEAK_1000_MAX_GIVEBACK",
        float,
        220.0,
    )
    RUNNER_GIVEBACK_PEAK_2000_MAX_GIVEBACK: float = _num_env(
        "RUNNER_GIVEBACK_PEAK_2000_MAX_GIVEBACK",
        float,
        450.0,
    )
    RUNNER_GIVEBACK_CLOSE_REMAINING: bool = _bool_env("RUNNER_GIVEBACK_CLOSE_REMAINING", True)
    RUNNER_TURBO_MONITOR_ENABLED: bool = _bool_env("RUNNER_TURBO_MONITOR_ENABLED", True)
    RUNNER_TURBO_PEAK_PCT: float = _num_env("RUNNER_TURBO_PEAK_PCT", float, 100.0)
    RUNNER_TURBO_INTERVAL_S: float = _num_env("RUNNER_TURBO_INTERVAL_S", float, 1.0)
    RUNNER_TURBO_MAX_DURATION_MIN: float = _num_env("RUNNER_TURBO_MAX_DURATION_MIN", float, 20.0)
    RUNNER_TURBO_PAPER_ONLY: bool = _bool_env("RUNNER_TURBO_PAPER_ONLY", True)
    PRE_PARTIAL_TIME_STOP_MIN: float = _num_env("PRE_PARTIAL_TIME_STOP_MIN", float, 0.0)
    PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT: float = _num_env("PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT", float, 0.0)
    PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT: float = _num_env("PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT", float, 0.0)
    PRE_PARTIAL_RETRACE_TRIGGER_PCT: float = _num_env("PRE_PARTIAL_RETRACE_TRIGGER_PCT", float, 0.0)
    PRE_PARTIAL_RETRACE_GIVEBACK_PCT: float = _num_env("PRE_PARTIAL_RETRACE_GIVEBACK_PCT", float, 0.0)
    PRE_PARTIAL_RETRACE_FLOOR_PCT: float = _num_env("PRE_PARTIAL_RETRACE_FLOOR_PCT", float, 0.0)
    NO_PUMP_WINDOW_MIN: float = _num_env("NO_PUMP_WINDOW_MIN", float, 0.0)
    NO_PUMP_MIN_PNL_PCT: float = _num_env_multi(
        ["NO_PUMP_MIN_PNL_PCT", "NO_PUMP_MIN_PEAK_PCT"],
        float,
        5.0,
    )
    NO_PUMP_MAX_PNL_PCT: float | None = _opt_num_env("NO_PUMP_MAX_PNL_PCT", float)
    TIME_STOP_MIN: float = _num_env("TIME_STOP_MIN", float, 0.0)
    TIME_STOP_MAX_PNL_PCT: float = _num_env("TIME_STOP_MAX_PNL_PCT", float, 2.0)
    TIME_STOP_MIN_PEAK_PCT: float = _num_env("TIME_STOP_MIN_PEAK_PCT", float, 5.0)
    EXIT_PROFILE_BY_REGIME: bool = _bool_env("EXIT_PROFILE_BY_REGIME", False)

    # Overrides opcionales de exits por regimen (solo se aplican si EXIT_PROFILE_BY_REGIME=true)
    PUMP_EARLY_TRAILING_PCT: float | None = _opt_num_env("PUMP_EARLY_TRAILING_PCT", float)
    PUMP_EARLY_TAKE_PROFIT_PCT: float | None = _opt_num_env("PUMP_EARLY_TAKE_PROFIT_PCT", float)
    PUMP_EARLY_STOP_LOSS_PCT: float | None = _opt_num_env("PUMP_EARLY_STOP_LOSS_PCT", float)
    PUMP_EARLY_MAX_HOLDING_H: float | None = _opt_num_env("PUMP_EARLY_MAX_HOLDING_H", float)
    PUMP_EARLY_MAX_HARD_HOLD_H: float | None = _opt_num_env("PUMP_EARLY_MAX_HARD_HOLD_H", float)
    PUMP_EARLY_TP_PARTIAL_TRIGGER_PCT: float | None = _opt_num_env("PUMP_EARLY_TP_PARTIAL_TRIGGER_PCT", float)
    PUMP_EARLY_TP_PARTIAL_FRACTION: float | None = _opt_num_env("PUMP_EARLY_TP_PARTIAL_FRACTION", float)
    PUMP_EARLY_POST_PARTIAL_STOP_PCT: float | None = _opt_num_env("PUMP_EARLY_POST_PARTIAL_STOP_PCT", float)
    PUMP_EARLY_POST_PARTIAL_TRAILING_PCT: float | None = _opt_num_env("PUMP_EARLY_POST_PARTIAL_TRAILING_PCT", float)
    PUMP_EARLY_POST_PARTIAL_PROTECTION_ENABLED: bool | None = _opt_bool_env("PUMP_EARLY_POST_PARTIAL_PROTECTION_ENABLED")
    PUMP_EARLY_POST_PARTIAL_LOCK_FLOOR_PCT: float | None = _opt_num_env("PUMP_EARLY_POST_PARTIAL_LOCK_FLOOR_PCT", float)
    PUMP_EARLY_POST_PARTIAL_MAX_GIVEBACK_PCT: float | None = _opt_num_env("PUMP_EARLY_POST_PARTIAL_MAX_GIVEBACK_PCT", float)
    PUMP_EARLY_PRE_PARTIAL_TIME_STOP_MIN: float | None = _opt_num_env("PUMP_EARLY_PRE_PARTIAL_TIME_STOP_MIN", float)
    PUMP_EARLY_PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT: float | None = _opt_num_env(
        "PUMP_EARLY_PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT",
        float,
    )
    PUMP_EARLY_PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT: float | None = _opt_num_env(
        "PUMP_EARLY_PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT",
        float,
    )
    PUMP_EARLY_PRE_PARTIAL_RETRACE_TRIGGER_PCT: float | None = _opt_num_env(
        "PUMP_EARLY_PRE_PARTIAL_RETRACE_TRIGGER_PCT",
        float,
    )
    PUMP_EARLY_PRE_PARTIAL_RETRACE_GIVEBACK_PCT: float | None = _opt_num_env(
        "PUMP_EARLY_PRE_PARTIAL_RETRACE_GIVEBACK_PCT",
        float,
    )
    PUMP_EARLY_PRE_PARTIAL_RETRACE_FLOOR_PCT: float | None = _opt_num_env(
        "PUMP_EARLY_PRE_PARTIAL_RETRACE_FLOOR_PCT",
        float,
    )
    PUMP_EARLY_NO_PUMP_WINDOW_MIN: float | None = _opt_num_env("PUMP_EARLY_NO_PUMP_WINDOW_MIN", float)
    PUMP_EARLY_NO_PUMP_MIN_PNL_PCT: float | None = _opt_num_env("PUMP_EARLY_NO_PUMP_MIN_PNL_PCT", float)
    PUMP_EARLY_NO_PUMP_MAX_PNL_PCT: float | None = _opt_num_env("PUMP_EARLY_NO_PUMP_MAX_PNL_PCT", float)
    PUMP_EARLY_TIME_STOP_MIN: float | None = _opt_num_env("PUMP_EARLY_TIME_STOP_MIN", float)
    PUMP_EARLY_TIME_STOP_MAX_PNL_PCT: float | None = _opt_num_env("PUMP_EARLY_TIME_STOP_MAX_PNL_PCT", float)
    PUMP_EARLY_TIME_STOP_MIN_PEAK_PCT: float | None = _opt_num_env("PUMP_EARLY_TIME_STOP_MIN_PEAK_PCT", float)

    DEX_MATURE_TRAILING_PCT: float | None = _opt_num_env("DEX_MATURE_TRAILING_PCT", float)
    DEX_MATURE_TAKE_PROFIT_PCT: float | None = _opt_num_env("DEX_MATURE_TAKE_PROFIT_PCT", float)
    DEX_MATURE_STOP_LOSS_PCT: float | None = _opt_num_env("DEX_MATURE_STOP_LOSS_PCT", float)
    DEX_MATURE_MAX_HOLDING_H: float | None = _opt_num_env("DEX_MATURE_MAX_HOLDING_H", float)
    DEX_MATURE_MAX_HARD_HOLD_H: float | None = _opt_num_env("DEX_MATURE_MAX_HARD_HOLD_H", float)
    DEX_MATURE_TP_PARTIAL_TRIGGER_PCT: float | None = _opt_num_env("DEX_MATURE_TP_PARTIAL_TRIGGER_PCT", float)
    DEX_MATURE_TP_PARTIAL_FRACTION: float | None = _opt_num_env("DEX_MATURE_TP_PARTIAL_FRACTION", float)
    DEX_MATURE_POST_PARTIAL_STOP_PCT: float | None = _opt_num_env("DEX_MATURE_POST_PARTIAL_STOP_PCT", float)
    DEX_MATURE_POST_PARTIAL_TRAILING_PCT: float | None = _opt_num_env("DEX_MATURE_POST_PARTIAL_TRAILING_PCT", float)
    DEX_MATURE_POST_PARTIAL_PROTECTION_ENABLED: bool | None = _opt_bool_env("DEX_MATURE_POST_PARTIAL_PROTECTION_ENABLED")
    DEX_MATURE_POST_PARTIAL_LOCK_FLOOR_PCT: float | None = _opt_num_env("DEX_MATURE_POST_PARTIAL_LOCK_FLOOR_PCT", float)
    DEX_MATURE_POST_PARTIAL_MAX_GIVEBACK_PCT: float | None = _opt_num_env("DEX_MATURE_POST_PARTIAL_MAX_GIVEBACK_PCT", float)
    DEX_MATURE_PRE_PARTIAL_TIME_STOP_MIN: float | None = _opt_num_env("DEX_MATURE_PRE_PARTIAL_TIME_STOP_MIN", float)
    DEX_MATURE_PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT: float | None = _opt_num_env(
        "DEX_MATURE_PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT",
        float,
    )
    DEX_MATURE_PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT: float | None = _opt_num_env(
        "DEX_MATURE_PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT",
        float,
    )
    DEX_MATURE_PRE_PARTIAL_RETRACE_TRIGGER_PCT: float | None = _opt_num_env(
        "DEX_MATURE_PRE_PARTIAL_RETRACE_TRIGGER_PCT",
        float,
    )
    DEX_MATURE_PRE_PARTIAL_RETRACE_GIVEBACK_PCT: float | None = _opt_num_env(
        "DEX_MATURE_PRE_PARTIAL_RETRACE_GIVEBACK_PCT",
        float,
    )
    DEX_MATURE_PRE_PARTIAL_RETRACE_FLOOR_PCT: float | None = _opt_num_env(
        "DEX_MATURE_PRE_PARTIAL_RETRACE_FLOOR_PCT",
        float,
    )
    DEX_MATURE_NO_PUMP_WINDOW_MIN: float | None = _opt_num_env("DEX_MATURE_NO_PUMP_WINDOW_MIN", float)
    DEX_MATURE_NO_PUMP_MIN_PNL_PCT: float | None = _opt_num_env("DEX_MATURE_NO_PUMP_MIN_PNL_PCT", float)
    DEX_MATURE_NO_PUMP_MAX_PNL_PCT: float | None = _opt_num_env("DEX_MATURE_NO_PUMP_MAX_PNL_PCT", float)
    DEX_MATURE_TIME_STOP_MIN: float | None = _opt_num_env("DEX_MATURE_TIME_STOP_MIN", float)
    DEX_MATURE_TIME_STOP_MAX_PNL_PCT: float | None = _opt_num_env("DEX_MATURE_TIME_STOP_MAX_PNL_PCT", float)
    DEX_MATURE_TIME_STOP_MIN_PEAK_PCT: float | None = _opt_num_env("DEX_MATURE_TIME_STOP_MIN_PEAK_PCT", float)

    REVIVAL_TRAILING_PCT: float | None = _opt_num_env("REVIVAL_TRAILING_PCT", float)
    REVIVAL_TAKE_PROFIT_PCT: float | None = _opt_num_env("REVIVAL_TAKE_PROFIT_PCT", float)
    REVIVAL_STOP_LOSS_PCT: float | None = _opt_num_env("REVIVAL_STOP_LOSS_PCT", float)
    REVIVAL_MAX_HOLDING_H: float | None = _opt_num_env("REVIVAL_MAX_HOLDING_H", float)
    REVIVAL_MAX_HARD_HOLD_H: float | None = _opt_num_env("REVIVAL_MAX_HARD_HOLD_H", float)
    REVIVAL_TP_PARTIAL_TRIGGER_PCT: float | None = _opt_num_env("REVIVAL_TP_PARTIAL_TRIGGER_PCT", float)
    REVIVAL_TP_PARTIAL_FRACTION: float | None = _opt_num_env("REVIVAL_TP_PARTIAL_FRACTION", float)
    REVIVAL_POST_PARTIAL_STOP_PCT: float | None = _opt_num_env("REVIVAL_POST_PARTIAL_STOP_PCT", float)
    REVIVAL_POST_PARTIAL_TRAILING_PCT: float | None = _opt_num_env("REVIVAL_POST_PARTIAL_TRAILING_PCT", float)
    REVIVAL_POST_PARTIAL_PROTECTION_ENABLED: bool | None = _opt_bool_env("REVIVAL_POST_PARTIAL_PROTECTION_ENABLED")
    REVIVAL_POST_PARTIAL_LOCK_FLOOR_PCT: float | None = _opt_num_env("REVIVAL_POST_PARTIAL_LOCK_FLOOR_PCT", float)
    REVIVAL_POST_PARTIAL_MAX_GIVEBACK_PCT: float | None = _opt_num_env("REVIVAL_POST_PARTIAL_MAX_GIVEBACK_PCT", float)
    REVIVAL_PRE_PARTIAL_TIME_STOP_MIN: float | None = _opt_num_env("REVIVAL_PRE_PARTIAL_TIME_STOP_MIN", float)
    REVIVAL_PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT: float | None = _opt_num_env(
        "REVIVAL_PRE_PARTIAL_TIME_STOP_MAX_PNL_PCT",
        float,
    )
    REVIVAL_PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT: float | None = _opt_num_env(
        "REVIVAL_PRE_PARTIAL_TIME_STOP_MIN_PEAK_PCT",
        float,
    )
    REVIVAL_PRE_PARTIAL_RETRACE_TRIGGER_PCT: float | None = _opt_num_env(
        "REVIVAL_PRE_PARTIAL_RETRACE_TRIGGER_PCT",
        float,
    )
    REVIVAL_PRE_PARTIAL_RETRACE_GIVEBACK_PCT: float | None = _opt_num_env(
        "REVIVAL_PRE_PARTIAL_RETRACE_GIVEBACK_PCT",
        float,
    )
    REVIVAL_PRE_PARTIAL_RETRACE_FLOOR_PCT: float | None = _opt_num_env(
        "REVIVAL_PRE_PARTIAL_RETRACE_FLOOR_PCT",
        float,
    )
    REVIVAL_NO_PUMP_WINDOW_MIN: float | None = _opt_num_env("REVIVAL_NO_PUMP_WINDOW_MIN", float)
    REVIVAL_NO_PUMP_MIN_PNL_PCT: float | None = _opt_num_env("REVIVAL_NO_PUMP_MIN_PNL_PCT", float)
    REVIVAL_NO_PUMP_MAX_PNL_PCT: float | None = _opt_num_env("REVIVAL_NO_PUMP_MAX_PNL_PCT", float)
    REVIVAL_TIME_STOP_MIN: float | None = _opt_num_env("REVIVAL_TIME_STOP_MIN", float)
    REVIVAL_TIME_STOP_MAX_PNL_PCT: float | None = _opt_num_env("REVIVAL_TIME_STOP_MAX_PNL_PCT", float)
    REVIVAL_TIME_STOP_MIN_PEAK_PCT: float | None = _opt_num_env("REVIVAL_TIME_STOP_MIN_PEAK_PCT", float)

    # ------- etiquetado posiciones ---------------------------------
    WIN_PCT: float = _WIN_PCT_VALUE
    ML_POSITIVE_PNL_PCT: float = _ML_POSITIVE_PNL_PCT_VALUE
    ML_POSITIVE_PNL_RATIO: float = _ML_POSITIVE_PNL_PCT_VALUE / 100.0
    LABEL_GRACE_H: int = _num_env("LABEL_GRACE_H", int, 2)

    # ------- temporizadores ----------------------------------------
    SLEEP_SECONDS: int = _num_env("SLEEP_SECONDS", int, 3)
    DISCOVERY_INTERVAL: int = _num_env("DISCOVERY_INTERVAL", int, 45)
    VALIDATION_BATCH_SIZE: int = _num_env("VALIDATION_BATCH_SIZE", int, 30)
    MAX_CANDIDATES: int = _num_env("MAX_CANDIDATES", int, 0)
    MAX_QUEUE_SIZE: int = _num_env("MAX_QUEUE_SIZE", int, 300)

    # ------- control re-queues -------------------------------------
    INCOMPLETE_RETRIES: int = _num_env("INCOMPLETE_RETRIES", int, 3)
    MAX_RETRIES: int = _num_env("MAX_RETRIES", int, 5)

    # ------- estrategia avanzada -----------------------------------
    BUY_FROM_CURVE: bool = _bool_env("BUY_FROM_CURVE", False)
    CURVE_BUY_RANK_MAX: int = _num_env("CURVE_BUY_RANK_MAX", int, 40)
    CURVE_MAX_COST: float = _num_env("CURVE_MAX_COST", float, 1.0)
    REVIVAL_LIQ_USD: float = _num_env("REVIVAL_LIQ_USD", float, 250.0)
    REVIVAL_VOL1H_USD: float = _num_env("REVIVAL_VOL1H_USD", float, 150.0)
    REVIVAL_PC_5M: float = _num_env("REVIVAL_PC_5M", float, 15.0)

    # ------- base de datos -----------------------------------------
    SQLITE_DB: str = os.getenv("SQLITE_DB", "data/memebotdatabase.db")

    # ------- logging -----------------------------------------------
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_PATH: pathlib.Path = pathlib.Path(os.getenv("LOG_PATH", PROJECT_ROOT / "logs"))

    # ------- wallet / black-lists ----------------------------------
    SOL_PUBLIC_KEY: str | None = os.getenv("SOL_PUBLIC_KEY")
    BANNED_CREATORS: tuple[str, ...] = tuple(
        x.strip() for x in (os.getenv("BANNED_CREATORS", "") or "").split(",") if x.strip()
    )


# instancia global inmutable
CFG = _Config()

# Crear carpeta de logs si procede (side-effect benigno)
try:
    CFG.LOG_PATH.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


# ───────────────────── exports legacy / conveniencia ─────────────────────
# Safety / optimization lock
DRY_RUN = CFG.DRY_RUN
STRATEGY_OPTIMIZATION_LOCK = CFG.STRATEGY_OPTIMIZATION_LOCK
AUTO_PROMOTE_LIVE = CFG.AUTO_PROMOTE_LIVE
MODEL_AUTO_PROMOTE = CFG.MODEL_AUTO_PROMOTE
LLM_TRADING_ENABLED = CFG.LLM_TRADING_ENABLED
ALLOW_LIVE_POLICY_ENFORCE = CFG.ALLOW_LIVE_POLICY_ENFORCE
REQUIRE_ENTRY_LANE_FOR_BUY = CFG.REQUIRE_ENTRY_LANE_FOR_BUY
ALLOW_UNTAGGED_STANDARD_BUY = CFG.ALLOW_UNTAGGED_STANDARD_BUY
DEX_MATURE_STANDARD_BUY_ENABLED = CFG.DEX_MATURE_STANDARD_BUY_ENABLED
PUMPFUN_STANDARD_BUY_ENABLED = CFG.PUMPFUN_STANDARD_BUY_ENABLED
UNTAGGED_BUY_SHADOW_ENABLED = CFG.UNTAGGED_BUY_SHADOW_ENABLED
LIVE_CANARY_ENABLED = CFG.LIVE_CANARY_ENABLED
LIVE_CANARY_MANUAL_APPROVAL = CFG.LIVE_CANARY_MANUAL_APPROVAL
LIVE_REQUIRE_ROUTE = CFG.LIVE_REQUIRE_ROUTE
LIVE_REQUIRE_PROVIDER_HEALTH = CFG.LIVE_REQUIRE_PROVIDER_HEALTH
LIVE_CANARY_MAX_OPEN = CFG.LIVE_CANARY_MAX_OPEN
LIVE_CANARY_MAX_DAILY_BUYS = CFG.LIVE_CANARY_MAX_DAILY_BUYS
LIVE_CANARY_DAILY_LOSS_CAP_SOL = CFG.LIVE_CANARY_DAILY_LOSS_CAP_SOL
LIVE_CANARY_SIZE_SOL = CFG.LIVE_CANARY_SIZE_SOL

# TTL Dex
DEXS_TTL_NIL = CFG.DEXS_TTL_NIL
DEXS_TTL_OK = CFG.DEXS_TTL_OK

# Dex/Gecko
DEX_API_BASE = CFG.DEXSCREENER_API
DEXSCREENER_API = CFG.DEXSCREENER_API
USE_GECKO_TERMINAL = CFG.USE_GECKO_TERMINAL
GECKO_API_URL = CFG.GECKO_API_URL

# Jupiter Price v3 (Lite)
USE_JUPITER_PRICE = CFG.USE_JUPITER_PRICE
JUPITER_PRICE_URL = CFG.JUPITER_PRICE_URL
JUPITER_RPM = CFG.JUPITER_RPM
JUPITER_TTL_NIL_SHORT = CFG.JUPITER_TTL_NIL_SHORT
JUPITER_TTL_NIL_MAX = CFG.JUPITER_TTL_NIL_MAX
JUPITER_TTL_OK = CFG.JUPITER_TTL_OK

# Jupiter Impact
USE_JUPITER_IMPACT = CFG.USE_JUPITER_IMPACT
IMPACT_PROBE_SOL = CFG.IMPACT_PROBE_SOL
IMPACT_MAX_PCT = CFG.IMPACT_MAX_PCT

# Helius / otros
HELIUS_API_BASE = CFG.HELIUS_REST_BASE
HELIUS_RPC_URL = CFG.HELIUS_RPC_URL
HELIUS_API_KEY = CFG.HELIUS_API_KEY
RUGCHECK_API_BASE = CFG.RUGCHECK_API_BASE
RUGCHECK_API_KEY = CFG.RUGCHECK_API_KEY
BITQUERY_TOKEN = CFG.BITQUERY_TOKEN
PUMPFUN_PROGRAM = CFG.PUMPFUN_PROGRAM

# Filtros
MAX_AGE_DAYS = CFG.MAX_AGE_DAYS
MIN_AGE_MIN = CFG.MIN_AGE_MIN
MIN_HOLDERS = CFG.MIN_HOLDERS
MIN_LIQUIDITY_USD = CFG.MIN_LIQUIDITY_USD
MIN_VOL_USD_24H = CFG.MIN_VOL_USD_24H
MIN_MARKET_CAP_USD = CFG.MIN_MARKET_CAP_USD
MAX_MARKET_CAP_USD = CFG.MAX_MARKET_CAP_USD
MAX_24H_VOLUME = CFG.MAX_24H_VOLUME
MIN_SCORE_TOTAL = CFG.MIN_SCORE_TOTAL
MAX_ACTIVE_POSITIONS = CFG.MAX_ACTIVE_POSITIONS
MAX_QUEUE_SIZE = CFG.MAX_QUEUE_SIZE
DEXS_TXNS_5M_MIN = CFG.DEXS_TXNS_5M_MIN
FILTER_PROFILE_BY_DISCOVERY = CFG.FILTER_PROFILE_BY_DISCOVERY
SNAPSHOT_QUALITY_FILTER_ENABLED = CFG.SNAPSHOT_QUALITY_FILTER_ENABLED
SNAPSHOT_MAX_MISSING_FIELDS = CFG.SNAPSHOT_MAX_MISSING_FIELDS
SNAPSHOT_REQUIRE_ACTIVITY_SIGNAL = CFG.SNAPSHOT_REQUIRE_ACTIVITY_SIGNAL
SNAPSHOT_REQUIRE_SOCIAL_OR_TREND = CFG.SNAPSHOT_REQUIRE_SOCIAL_OR_TREND
SNAPSHOT_REQUIRE_RUG_SCORE = CFG.SNAPSHOT_REQUIRE_RUG_SCORE
SNAPSHOT_ALLOWED_PRICE_SOURCES = CFG.SNAPSHOT_ALLOWED_PRICE_SOURCES
TOXIC_INITIAL_SELL_PRESSURE_TTL_S = CFG.TOXIC_INITIAL_SELL_PRESSURE_TTL_S

DEX_MIN_AGE_MIN = CFG.DEX_MIN_AGE_MIN
DEX_MIN_HOLDERS = CFG.DEX_MIN_HOLDERS
DEX_MIN_LIQUIDITY_USD = CFG.DEX_MIN_LIQUIDITY_USD
DEX_MIN_VOL_USD_24H = CFG.DEX_MIN_VOL_USD_24H
DEX_MIN_MARKET_CAP_USD = CFG.DEX_MIN_MARKET_CAP_USD
DEX_MAX_MARKET_CAP_USD = CFG.DEX_MAX_MARKET_CAP_USD
DEX_BUY_SOFT_SCORE_MIN = CFG.DEX_BUY_SOFT_SCORE_MIN
DEX_AI_THRESHOLD = CFG.DEX_AI_THRESHOLD
DEX_REQUIRE_JUPITER_FOR_BUY = CFG.DEX_REQUIRE_JUPITER_FOR_BUY

PUMPFUN_MIN_AGE_MIN = CFG.PUMPFUN_MIN_AGE_MIN
PUMPFUN_MIN_HOLDERS = CFG.PUMPFUN_MIN_HOLDERS
PUMPFUN_MIN_LIQUIDITY_USD = CFG.PUMPFUN_MIN_LIQUIDITY_USD
PUMPFUN_MIN_VOL_USD_24H = CFG.PUMPFUN_MIN_VOL_USD_24H
PUMPFUN_MIN_MARKET_CAP_USD = CFG.PUMPFUN_MIN_MARKET_CAP_USD
PUMPFUN_MAX_MARKET_CAP_USD = CFG.PUMPFUN_MAX_MARKET_CAP_USD
PUMPFUN_BUY_SOFT_SCORE_MIN = CFG.PUMPFUN_BUY_SOFT_SCORE_MIN
PUMPFUN_AI_THRESHOLD = CFG.PUMPFUN_AI_THRESHOLD
PUMPFUN_REQUIRE_JUPITER_FOR_BUY = CFG.PUMPFUN_REQUIRE_JUPITER_FOR_BUY

REVIVAL_MIN_AGE_MIN = CFG.REVIVAL_MIN_AGE_MIN
REVIVAL_MIN_HOLDERS = CFG.REVIVAL_MIN_HOLDERS
REVIVAL_MIN_LIQUIDITY_USD = CFG.REVIVAL_MIN_LIQUIDITY_USD
REVIVAL_MIN_VOL_USD_24H = CFG.REVIVAL_MIN_VOL_USD_24H
REVIVAL_MIN_MARKET_CAP_USD = CFG.REVIVAL_MIN_MARKET_CAP_USD
REVIVAL_MAX_MARKET_CAP_USD = CFG.REVIVAL_MAX_MARKET_CAP_USD
REVIVAL_BUY_SOFT_SCORE_MIN = CFG.REVIVAL_BUY_SOFT_SCORE_MIN
REVIVAL_AI_THRESHOLD = CFG.REVIVAL_AI_THRESHOLD
REVIVAL_REQUIRE_JUPITER_FOR_BUY = CFG.REVIVAL_REQUIRE_JUPITER_FOR_BUY
REGIME_PUMP_EARLY_MAX_AGE_MIN = CFG.REGIME_PUMP_EARLY_MAX_AGE_MIN
DYNAMIC_SIZING_ENABLED = CFG.DYNAMIC_SIZING_ENABLED
AI_SIZING_ENABLED = CFG.AI_SIZING_ENABLED
SIZE_MIN_MULTIPLIER = CFG.SIZE_MIN_MULTIPLIER
SIZE_MID_MULTIPLIER = CFG.SIZE_MID_MULTIPLIER
SIZE_MAX_MULTIPLIER = CFG.SIZE_MAX_MULTIPLIER
SIZE_ACCEPTABLE_MIN_POINTS = CFG.SIZE_ACCEPTABLE_MIN_POINTS
SIZE_PREMIUM_MIN_POINTS = CFG.SIZE_PREMIUM_MIN_POINTS
PUMP_EARLY_MAX_SIZE_MULTIPLIER = CFG.PUMP_EARLY_MAX_SIZE_MULTIPLIER
DEX_MATURE_MAX_SIZE_MULTIPLIER = CFG.DEX_MATURE_MAX_SIZE_MULTIPLIER
REVIVAL_MAX_SIZE_MULTIPLIER = CFG.REVIVAL_MAX_SIZE_MULTIPLIER
MAX_ACTIVE_POSITIONS_PER_REGIME = CFG.MAX_ACTIVE_POSITIONS_PER_REGIME
PUMP_EARLY_MAX_ACTIVE_POSITIONS = CFG.PUMP_EARLY_MAX_ACTIVE_POSITIONS
DEX_MATURE_MAX_ACTIVE_POSITIONS = CFG.DEX_MATURE_MAX_ACTIVE_POSITIONS
REVIVAL_MAX_ACTIVE_POSITIONS = CFG.REVIVAL_MAX_ACTIVE_POSITIONS
PAPER_AGGRESSIVE_TRADING_ENABLED = CFG.PAPER_AGGRESSIVE_TRADING_ENABLED
PAPER_AGGRESSIVE_CONFIRM_SNAPSHOTS = CFG.PAPER_AGGRESSIVE_CONFIRM_SNAPSHOTS
PAPER_AGGRESSIVE_CONFIRM_BACKOFF_S = CFG.PAPER_AGGRESSIVE_CONFIRM_BACKOFF_S
PAPER_AGGRESSIVE_MIN_AGE_MIN = CFG.PAPER_AGGRESSIVE_MIN_AGE_MIN
PAPER_AGGRESSIVE_MIN_LIQUIDITY_USD = CFG.PAPER_AGGRESSIVE_MIN_LIQUIDITY_USD
PAPER_AGGRESSIVE_MIN_MARKET_CAP_USD = CFG.PAPER_AGGRESSIVE_MIN_MARKET_CAP_USD
PAPER_AGGRESSIVE_MAX_MARKET_CAP_USD = CFG.PAPER_AGGRESSIVE_MAX_MARKET_CAP_USD
PAPER_AGGRESSIVE_MIN_SCORE_TOTAL = CFG.PAPER_AGGRESSIVE_MIN_SCORE_TOTAL
PAPER_AGGRESSIVE_MIN_RANK_SCORE = CFG.PAPER_AGGRESSIVE_MIN_RANK_SCORE
PAPER_AGGRESSIVE_MIN_TXNS_5M = CFG.PAPER_AGGRESSIVE_MIN_TXNS_5M
PAPER_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS = CFG.PAPER_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS
PAPER_AGGRESSIVE_MAX_PRICE_IMPACT_PCT = CFG.PAPER_AGGRESSIVE_MAX_PRICE_IMPACT_PCT
PAPER_AGGRESSIVE_REQUIRE_ROUTE = CFG.PAPER_AGGRESSIVE_REQUIRE_ROUTE
PAPER_AGGRESSIVE_REQUIRE_PRICE = CFG.PAPER_AGGRESSIVE_REQUIRE_PRICE
PAPER_AGGRESSIVE_BUY_RESEARCH_LANES = CFG.PAPER_AGGRESSIVE_BUY_RESEARCH_LANES
LIVE_AGGRESSIVE_TRADING_ENABLED = CFG.LIVE_AGGRESSIVE_TRADING_ENABLED
LIVE_AGGRESSIVE_CONFIRM_SNAPSHOTS = CFG.LIVE_AGGRESSIVE_CONFIRM_SNAPSHOTS
LIVE_AGGRESSIVE_CONFIRM_BACKOFF_S = CFG.LIVE_AGGRESSIVE_CONFIRM_BACKOFF_S
LIVE_AGGRESSIVE_MIN_AGE_MIN = CFG.LIVE_AGGRESSIVE_MIN_AGE_MIN
LIVE_AGGRESSIVE_MIN_LIQUIDITY_USD = CFG.LIVE_AGGRESSIVE_MIN_LIQUIDITY_USD
LIVE_AGGRESSIVE_MIN_MARKET_CAP_USD = CFG.LIVE_AGGRESSIVE_MIN_MARKET_CAP_USD
LIVE_AGGRESSIVE_MAX_MARKET_CAP_USD = CFG.LIVE_AGGRESSIVE_MAX_MARKET_CAP_USD
LIVE_AGGRESSIVE_MIN_SCORE_TOTAL = CFG.LIVE_AGGRESSIVE_MIN_SCORE_TOTAL
LIVE_AGGRESSIVE_MIN_RANK_SCORE = CFG.LIVE_AGGRESSIVE_MIN_RANK_SCORE
LIVE_AGGRESSIVE_MIN_TXNS_5M = CFG.LIVE_AGGRESSIVE_MIN_TXNS_5M
LIVE_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS = CFG.LIVE_AGGRESSIVE_MAX_SNAPSHOT_MISSING_FIELDS
LIVE_AGGRESSIVE_MAX_PRICE_IMPACT_PCT = CFG.LIVE_AGGRESSIVE_MAX_PRICE_IMPACT_PCT
LIVE_AGGRESSIVE_REQUIRE_ROUTE = CFG.LIVE_AGGRESSIVE_REQUIRE_ROUTE
LIVE_AGGRESSIVE_REQUIRE_PRICE = CFG.LIVE_AGGRESSIVE_REQUIRE_PRICE
LIVE_AGGRESSIVE_BUY_RESEARCH_LANES = CFG.LIVE_AGGRESSIVE_BUY_RESEARCH_LANES
LIVE_AGGRESSIVE_CONTINUE_ON_HEALTH = CFG.LIVE_AGGRESSIVE_CONTINUE_ON_HEALTH
LIVE_AGGRESSIVE_HEALTH_SIZE_CAP_MULTIPLIER = CFG.LIVE_AGGRESSIVE_HEALTH_SIZE_CAP_MULTIPLIER
PUMP_EARLY_LIVE_HARD_MAX_MARKET_CAP_USD = CFG.PUMP_EARLY_LIVE_HARD_MAX_MARKET_CAP_USD
PUMP_EARLY_LIVE_HARD_MAX_PRICE_IMPACT_PCT = CFG.PUMP_EARLY_LIVE_HARD_MAX_PRICE_IMPACT_PCT
PUMP_EARLY_LIVE_MAX_SNAPSHOT_MISSING_FIELDS = CFG.PUMP_EARLY_LIVE_MAX_SNAPSHOT_MISSING_FIELDS
PAPER_COLD_START_ENABLED = CFG.PAPER_COLD_START_ENABLED
PAPER_COLD_START_MAX_CLOSED_TRADES = CFG.PAPER_COLD_START_MAX_CLOSED_TRADES
PAPER_COLD_START_MIN_AGE_MIN = CFG.PAPER_COLD_START_MIN_AGE_MIN
PAPER_COLD_START_MIN_SCORE_TOTAL = CFG.PAPER_COLD_START_MIN_SCORE_TOTAL
PAPER_COLD_START_MIN_LIQUIDITY_USD = CFG.PAPER_COLD_START_MIN_LIQUIDITY_USD
PAPER_COLD_START_MIN_MARKET_CAP_USD = CFG.PAPER_COLD_START_MIN_MARKET_CAP_USD
PAPER_COLD_START_MAX_SNAPSHOT_MISSING_FIELDS = CFG.PAPER_COLD_START_MAX_SNAPSHOT_MISSING_FIELDS
PAPER_COLD_START_MIN_RANK_SCORE = CFG.PAPER_COLD_START_MIN_RANK_SCORE
PAPER_COLD_START_REQUIRE_PRICE_PCT_5M = CFG.PAPER_COLD_START_REQUIRE_PRICE_PCT_5M
PAPER_COLD_START_MIN_PRICE_PCT_5M = CFG.PAPER_COLD_START_MIN_PRICE_PCT_5M
PAPER_COLD_START_MAX_PRICE_PCT_5M = CFG.PAPER_COLD_START_MAX_PRICE_PCT_5M
PAPER_COLD_START_SHADOW_PROBE_ENABLED = CFG.PAPER_COLD_START_SHADOW_PROBE_ENABLED
PAPER_COLD_START_SHADOW_PROBE_SIZE_MULTIPLIER = CFG.PAPER_COLD_START_SHADOW_PROBE_SIZE_MULTIPLIER
PUMP_EARLY_SNIPER_ENABLED = CFG.PUMP_EARLY_SNIPER_ENABLED
PUMP_EARLY_SNIPER_MODE = CFG.PUMP_EARLY_SNIPER_MODE
PUMP_EARLY_SNIPER_MIN_AGE_MIN = CFG.PUMP_EARLY_SNIPER_MIN_AGE_MIN
PUMP_EARLY_SNIPER_MAX_AGE_MIN = CFG.PUMP_EARLY_SNIPER_MAX_AGE_MIN
PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD = CFG.PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD
PUMP_EARLY_SNIPER_MICRO_MIN_LIQUIDITY_USD = CFG.PUMP_EARLY_SNIPER_MICRO_MIN_LIQUIDITY_USD
PUMP_EARLY_SNIPER_MICRO_MIN_VOLUME_USD_24H = CFG.PUMP_EARLY_SNIPER_MICRO_MIN_VOLUME_USD_24H
PUMP_EARLY_SNIPER_MIN_MARKET_CAP_USD = CFG.PUMP_EARLY_SNIPER_MIN_MARKET_CAP_USD
PUMP_EARLY_SNIPER_MAX_MARKET_CAP_USD = CFG.PUMP_EARLY_SNIPER_MAX_MARKET_CAP_USD
PUMP_EARLY_SNIPER_MICRO_MAX_MARKET_CAP_USD = CFG.PUMP_EARLY_SNIPER_MICRO_MAX_MARKET_CAP_USD
PUMP_EARLY_SNIPER_MIN_SCORE_TOTAL = CFG.PUMP_EARLY_SNIPER_MIN_SCORE_TOTAL
PUMP_EARLY_SNIPER_MICRO_MIN_SCORE_TOTAL = CFG.PUMP_EARLY_SNIPER_MICRO_MIN_SCORE_TOTAL
PUMP_EARLY_SNIPER_MIN_RANK_SCORE = CFG.PUMP_EARLY_SNIPER_MIN_RANK_SCORE
PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE = CFG.PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE
PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT = CFG.PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT
PUMP_EARLY_SNIPER_MICRO_MAX_PRICE_IMPACT_PCT = CFG.PUMP_EARLY_SNIPER_MICRO_MAX_PRICE_IMPACT_PCT
PUMP_EARLY_SNIPER_MIN_TXNS_5M = CFG.PUMP_EARLY_SNIPER_MIN_TXNS_5M
PUMP_EARLY_SNIPER_MICRO_MIN_TXNS_5M = CFG.PUMP_EARLY_SNIPER_MICRO_MIN_TXNS_5M
PUMP_EARLY_SNIPER_MIN_PRICE_PCT_5M = CFG.PUMP_EARLY_SNIPER_MIN_PRICE_PCT_5M
PUMP_EARLY_SNIPER_MAX_PRICE_PCT_5M = CFG.PUMP_EARLY_SNIPER_MAX_PRICE_PCT_5M
PUMP_EARLY_SNIPER_MICRO_MIN_PRICE_PCT_5M = CFG.PUMP_EARLY_SNIPER_MICRO_MIN_PRICE_PCT_5M
PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS = CFG.PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS
PUMP_EARLY_SNIPER_HOT_MIN_RANK_SCORE = CFG.PUMP_EARLY_SNIPER_HOT_MIN_RANK_SCORE
PUMP_EARLY_SNIPER_HOT_MIN_TXNS_5M = CFG.PUMP_EARLY_SNIPER_HOT_MIN_TXNS_5M
PUMP_EARLY_SNIPER_HOT_MIN_PRICE_PCT_5M = CFG.PUMP_EARLY_SNIPER_HOT_MIN_PRICE_PCT_5M
PUMP_EARLY_SNIPER_HOT_MAX_PRICE_PCT_5M = CFG.PUMP_EARLY_SNIPER_HOT_MAX_PRICE_PCT_5M
PUMP_EARLY_SNIPER_HOT_MAX_SNAPSHOT_MISSING_FIELDS = CFG.PUMP_EARLY_SNIPER_HOT_MAX_SNAPSHOT_MISSING_FIELDS
PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_AGE_MIN = CFG.PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_AGE_MIN
PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_TXNS_5M = CFG.PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_TXNS_5M
PUMP_EARLY_SNIPER_FAST_CONFIRM_BACKOFF_S = CFG.PUMP_EARLY_SNIPER_FAST_CONFIRM_BACKOFF_S
PUMP_EARLY_SNIPER_SIZE_MICRO_MULTIPLIER = CFG.PUMP_EARLY_SNIPER_SIZE_MICRO_MULTIPLIER
PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER = CFG.PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER
PUMP_EARLY_SNIPER_SIZE_HOT_MULTIPLIER = CFG.PUMP_EARLY_SNIPER_SIZE_HOT_MULTIPLIER
PUMP_EARLY_SNIPER_CANARY_INITIAL_CLOSES = CFG.PUMP_EARLY_SNIPER_CANARY_INITIAL_CLOSES
PUMP_EARLY_SNIPER_CANARY_INITIAL_SIZE_CAP = CFG.PUMP_EARLY_SNIPER_CANARY_INITIAL_SIZE_CAP
PUMP_EARLY_SNIPER_MAX_OPEN_PAPER = CFG.PUMP_EARLY_SNIPER_MAX_OPEN_PAPER
PUMP_EARLY_SNIPER_MAX_OPEN_LIVE_CANARY = CFG.PUMP_EARLY_SNIPER_MAX_OPEN_LIVE_CANARY
PUMP_EARLY_SNIPER_MAX_OPEN_LIVE_CANARY_ADVANCED = CFG.PUMP_EARLY_SNIPER_MAX_OPEN_LIVE_CANARY_ADVANCED
PUMP_EARLY_SNIPER_ADVANCED_MIN_CLOSED = CFG.PUMP_EARLY_SNIPER_ADVANCED_MIN_CLOSED
PUMP_EARLY_SNIPER_ADVANCED_MIN_AVG_PNL_PCT = CFG.PUMP_EARLY_SNIPER_ADVANCED_MIN_AVG_PNL_PCT
PUMP_EARLY_SNIPER_ADVANCED_MAX_LOSS_STREAK = CFG.PUMP_EARLY_SNIPER_ADVANCED_MAX_LOSS_STREAK
PUMP_EARLY_SNIPER_DEMOTE_LOSS_STREAK = CFG.PUMP_EARLY_SNIPER_DEMOTE_LOSS_STREAK
PUMP_EARLY_SNIPER_DEMOTE_WINDOW_TRADES = CFG.PUMP_EARLY_SNIPER_DEMOTE_WINDOW_TRADES
PUMP_EARLY_SNIPER_DEMOTE_AVG_PNL_PCT = CFG.PUMP_EARLY_SNIPER_DEMOTE_AVG_PNL_PCT
PUMP_EARLY_SNIPER_DEMOTE_LIQ_CRUSH_FIRST_CLOSES = CFG.PUMP_EARLY_SNIPER_DEMOTE_LIQ_CRUSH_FIRST_CLOSES
PUMP_EARLY_SNIPER_DEMOTE_LIQ_CRUSH_ROLLING = CFG.PUMP_EARLY_SNIPER_DEMOTE_LIQ_CRUSH_ROLLING
PUMP_EARLY_SNIPER_RECOVERY_MIN_PAPER_CLOSES = CFG.PUMP_EARLY_SNIPER_RECOVERY_MIN_PAPER_CLOSES
PUMP_EARLY_SNIPER_RECOVERY_MIN_AVG_PNL_PCT = CFG.PUMP_EARLY_SNIPER_RECOVERY_MIN_AVG_PNL_PCT
PUMP_EARLY_SNIPER_PAPER_CONTINUE_ON_HEALTH = CFG.PUMP_EARLY_SNIPER_PAPER_CONTINUE_ON_HEALTH
PUMP_EARLY_SNIPER_PAPER_RECOVERY_SIZE_CAP = CFG.PUMP_EARLY_SNIPER_PAPER_RECOVERY_SIZE_CAP
PUMP_EARLY_SNIPER_LIVE_CONTINUE_ON_HEALTH = CFG.PUMP_EARLY_SNIPER_LIVE_CONTINUE_ON_HEALTH
PUMP_EARLY_SNIPER_LIVE_RECOVERY_SIZE_CAP = CFG.PUMP_EARLY_SNIPER_LIVE_RECOVERY_SIZE_CAP
PUMP_EARLY_SNIPER_LIVE_REQUIRE_MANUAL_APPROVAL = CFG.PUMP_EARLY_SNIPER_LIVE_REQUIRE_MANUAL_APPROVAL
PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_ENABLED = CFG.PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_ENABLED
PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_MIN_AGE_MIN = CFG.PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_MIN_AGE_MIN
PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_USD = CFG.PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_USD
PUMP_EARLY_PROFIT_LANE_ENABLED = CFG.PUMP_EARLY_PROFIT_LANE_ENABLED
PUMP_EARLY_PROFIT_DEX_ALLOWLIST = CFG.PUMP_EARLY_PROFIT_DEX_ALLOWLIST
PUMP_EARLY_PROFIT_REQUIRE_REAL_LIQUIDITY = CFG.PUMP_EARLY_PROFIT_REQUIRE_REAL_LIQUIDITY
PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD = CFG.PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD
PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL = CFG.PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL
PUMP_EARLY_PROFIT_MIN_AGE_MIN = CFG.PUMP_EARLY_PROFIT_MIN_AGE_MIN
PUMP_EARLY_PROFIT_MAX_AGE_MIN = CFG.PUMP_EARLY_PROFIT_MAX_AGE_MIN
PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT = CFG.PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT
PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD = CFG.PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD
PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD = CFG.PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD
PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES = CFG.PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES
PUMP_EARLY_AGGRESSIVE_RESEARCH_GUARD_ENABLED = CFG.PUMP_EARLY_AGGRESSIVE_RESEARCH_GUARD_ENABLED
PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PRICE5M_RANGES = CFG.PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PRICE5M_RANGES
PUMP_EARLY_AGGRESSIVE_RESEARCH_DEX_ALLOWLIST = CFG.PUMP_EARLY_AGGRESSIVE_RESEARCH_DEX_ALLOWLIST
PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_HIGH_MCAP_USD = CFG.PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_HIGH_MCAP_USD
PUMP_EARLY_AGGRESSIVE_RESEARCH_HIGH_MCAP_ALLOW_MIN_TXNS_5M = CFG.PUMP_EARLY_AGGRESSIVE_RESEARCH_HIGH_MCAP_ALLOW_MIN_TXNS_5M
PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PROXY = CFG.PUMP_EARLY_AGGRESSIVE_RESEARCH_BLOCK_PROXY
PUMP_EARLY_AGGRESSIVE_RESEARCH_HOT_PRICE5M_MIN_PCT = CFG.PUMP_EARLY_AGGRESSIVE_RESEARCH_HOT_PRICE5M_MIN_PCT
PUMP_EARLY_AGGRESSIVE_RESEARCH_HOT_MIN_TXNS_5M = CFG.PUMP_EARLY_AGGRESSIVE_RESEARCH_HOT_MIN_TXNS_5M
HOT_QUEUE_ENABLED = CFG.HOT_QUEUE_ENABLED
HOT_QUEUE_MAX_SIZE = CFG.HOT_QUEUE_MAX_SIZE
HOT_QUEUE_BATCH_SIZE = CFG.HOT_QUEUE_BATCH_SIZE
HOT_QUEUE_MAX_AGE_MIN = CFG.HOT_QUEUE_MAX_AGE_MIN
HOT_QUEUE_DEDUP_TTL_S = CFG.HOT_QUEUE_DEDUP_TTL_S
HOT_QUEUE_PRIORITY_SOURCES = CFG.HOT_QUEUE_PRIORITY_SOURCES
FAST_ENRICHMENT_ENABLED = CFG.FAST_ENRICHMENT_ENABLED
GREEN_SNIPER_ENABLED = CFG.GREEN_SNIPER_ENABLED
GREEN_SNIPER_LIVE_ENABLED = CFG.GREEN_SNIPER_LIVE_ENABLED
GREEN_SNIPER_LIVE_SIZE_SOL = CFG.GREEN_SNIPER_LIVE_SIZE_SOL
GREEN_SNIPER_LIVE_MAX_OPEN = CFG.GREEN_SNIPER_LIVE_MAX_OPEN
GREEN_SNIPER_ML_BLOCK_ENABLED = CFG.GREEN_SNIPER_ML_BLOCK_ENABLED
BIRTH_PROBE_MICRO_CANARY_ENABLED = CFG.BIRTH_PROBE_MICRO_CANARY_ENABLED
BIRTH_PROBE_MICRO_CANARY_PAPER_ENABLED = CFG.BIRTH_PROBE_MICRO_CANARY_PAPER_ENABLED
BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED = CFG.BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED
BIRTH_PROBE_MICRO_CANARY_AMOUNT_SOL = CFG.BIRTH_PROBE_MICRO_CANARY_AMOUNT_SOL
BIRTH_PROBE_MICRO_CANARY_MAX_OPEN = CFG.BIRTH_PROBE_MICRO_CANARY_MAX_OPEN
BIRTH_PROBE_MICRO_CANARY_MAX_DAILY_BUYS = CFG.BIRTH_PROBE_MICRO_CANARY_MAX_DAILY_BUYS
BIRTH_PROBE_MICRO_CANARY_ALLOWED_REASON_GROUPS = CFG.BIRTH_PROBE_MICRO_CANARY_ALLOWED_REASON_GROUPS
BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_EV_PCT = CFG.BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_EV_PCT
BIRTH_PROBE_MICRO_CANARY_PNL_CAP_PCT = CFG.BIRTH_PROBE_MICRO_CANARY_PNL_CAP_PCT
BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_CAPPED_EV_PCT = CFG.BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_CAPPED_EV_PCT
BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_SAMPLES = CFG.BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_SAMPLES
BIRTH_PROBE_MICRO_CANARY_TIME_STOP_MIN = CFG.BIRTH_PROBE_MICRO_CANARY_TIME_STOP_MIN
BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_EXIT_MIN = CFG.BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_EXIT_MIN
BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_MIN_PNL = CFG.BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_MIN_PNL
BIRTH_PROBE_MICRO_CANARY_TP1_PCT = CFG.BIRTH_PROBE_MICRO_CANARY_TP1_PCT
BIRTH_PROBE_MICRO_CANARY_TP1_FRACTION = CFG.BIRTH_PROBE_MICRO_CANARY_TP1_FRACTION
BIRTH_PROBE_MICRO_CANARY_TP2_PCT = CFG.BIRTH_PROBE_MICRO_CANARY_TP2_PCT
BIRTH_PROBE_MICRO_CANARY_TP2_FRACTION = CFG.BIRTH_PROBE_MICRO_CANARY_TP2_FRACTION
BIRTH_PROBE_MICRO_CANARY_TP3_PCT = CFG.BIRTH_PROBE_MICRO_CANARY_TP3_PCT
BIRTH_PROBE_MICRO_CANARY_TP3_FRACTION = CFG.BIRTH_PROBE_MICRO_CANARY_TP3_FRACTION
BIRTH_PROBE_MICRO_CANARY_TP4_PCT = CFG.BIRTH_PROBE_MICRO_CANARY_TP4_PCT
BIRTH_PROBE_MICRO_CANARY_TP4_FRACTION = CFG.BIRTH_PROBE_MICRO_CANARY_TP4_FRACTION
BIRTH_PROBE_MICRO_CANARY_MOONBAG_FRACTION = CFG.BIRTH_PROBE_MICRO_CANARY_MOONBAG_FRACTION
RESEARCH_RANK_CANARY_FORCE_OWN_LANE = CFG.RESEARCH_RANK_CANARY_FORCE_OWN_LANE
RESEARCH_RANK_CANARY_SHADOW_IF_NOT_EXECUTABLE = CFG.RESEARCH_RANK_CANARY_SHADOW_IF_NOT_EXECUTABLE
RESEARCH_RANK_CANARY_LOW_BAND_MIN_RANK_SCORE = CFG.RESEARCH_RANK_CANARY_LOW_BAND_MIN_RANK_SCORE
RESEARCH_RANK_CANARY_LOW_BAND_MIN_LIQUIDITY_USD = CFG.RESEARCH_RANK_CANARY_LOW_BAND_MIN_LIQUIDITY_USD
RESEARCH_RANK_CANARY_PRIORITY_MODE = CFG.RESEARCH_RANK_CANARY_PRIORITY_MODE
RESEARCH_RANK_CANARY_PRIORITY_MIN_TXNS_5M = CFG.RESEARCH_RANK_CANARY_PRIORITY_MIN_TXNS_5M
RESEARCH_RANK_CANARY_PRIORITY_MIN_LIQUIDITY_USD = CFG.RESEARCH_RANK_CANARY_PRIORITY_MIN_LIQUIDITY_USD
RESEARCH_RANK_CANARY_PRIORITY_MIN_PRICE5M = CFG.RESEARCH_RANK_CANARY_PRIORITY_MIN_PRICE5M
RESEARCH_RANK_CANARY_PRIORITY_MAX_PRICE5M = CFG.RESEARCH_RANK_CANARY_PRIORITY_MAX_PRICE5M
RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE = CFG.RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE
RESEARCH_RANK_CANARY_PRIORITY_MAX_OPEN = CFG.RESEARCH_RANK_CANARY_PRIORITY_MAX_OPEN
PAPER_EXPLORATION_QUOTA_ENABLED = CFG.PAPER_EXPLORATION_QUOTA_ENABLED
PAPER_EXPLORATION_MAX_DAILY_BUYS = CFG.PAPER_EXPLORATION_MAX_DAILY_BUYS
PAPER_EXPLORATION_MAX_OPEN = CFG.PAPER_EXPLORATION_MAX_OPEN
PAPER_EXPLORATION_AMOUNT_SOL = CFG.PAPER_EXPLORATION_AMOUNT_SOL
PAPER_EXPLORATION_IDLE_HOURS = CFG.PAPER_EXPLORATION_IDLE_HOURS
PAPER_SNIPER_MODE = CFG.PAPER_SNIPER_MODE
PUMP_EARLY_METEOR_PRIME_ENABLED = CFG.PUMP_EARLY_METEOR_PRIME_ENABLED
PUMP_EARLY_METEOR_PRIME_MIN_LIQUIDITY_USD = CFG.PUMP_EARLY_METEOR_PRIME_MIN_LIQUIDITY_USD
PUMP_EARLY_METEOR_PRIME_MAX_LIQUIDITY_USD = CFG.PUMP_EARLY_METEOR_PRIME_MAX_LIQUIDITY_USD
PUMP_EARLY_METEOR_PRIME_MIN_MARKET_CAP_USD = CFG.PUMP_EARLY_METEOR_PRIME_MIN_MARKET_CAP_USD
PUMP_EARLY_METEOR_PRIME_MAX_MARKET_CAP_USD = CFG.PUMP_EARLY_METEOR_PRIME_MAX_MARKET_CAP_USD
PUMP_EARLY_METEOR_PRIME_MIN_PRICE_PCT_5M = CFG.PUMP_EARLY_METEOR_PRIME_MIN_PRICE_PCT_5M
PUMP_EARLY_METEOR_PRIME_MAX_PRICE_PCT_5M = CFG.PUMP_EARLY_METEOR_PRIME_MAX_PRICE_PCT_5M
PUMP_EARLY_METEOR_PRIME_MIN_TXNS_5M = CFG.PUMP_EARLY_METEOR_PRIME_MIN_TXNS_5M
PUMP_EARLY_METEOR_PRIME_MIN_SCORE_TOTAL = CFG.PUMP_EARLY_METEOR_PRIME_MIN_SCORE_TOTAL
PUMP_EARLY_METEOR_PRIME_MIN_AGE_MIN = CFG.PUMP_EARLY_METEOR_PRIME_MIN_AGE_MIN
PUMP_EARLY_METEOR_PRIME_MAX_AGE_MIN = CFG.PUMP_EARLY_METEOR_PRIME_MAX_AGE_MIN
PUMP_EARLY_METEOR_PRIME_MAX_PRICE_IMPACT_PCT = CFG.PUMP_EARLY_METEOR_PRIME_MAX_PRICE_IMPACT_PCT
PUMP_EARLY_METEOR_PRIME_MIN_VOLUME_USD_24H = CFG.PUMP_EARLY_METEOR_PRIME_MIN_VOLUME_USD_24H
PUMP_EARLY_BREAKOUT_PROBE_ENABLED = CFG.PUMP_EARLY_BREAKOUT_PROBE_ENABLED
PUMP_EARLY_BREAKOUT_MIN_LIQUIDITY_USD = CFG.PUMP_EARLY_BREAKOUT_MIN_LIQUIDITY_USD
PUMP_EARLY_BREAKOUT_MAX_LIQUIDITY_USD = CFG.PUMP_EARLY_BREAKOUT_MAX_LIQUIDITY_USD
PUMP_EARLY_BREAKOUT_MIN_MARKET_CAP_USD = CFG.PUMP_EARLY_BREAKOUT_MIN_MARKET_CAP_USD
PUMP_EARLY_BREAKOUT_MAX_MARKET_CAP_USD = CFG.PUMP_EARLY_BREAKOUT_MAX_MARKET_CAP_USD
PUMP_EARLY_BREAKOUT_MIN_PRICE_PCT_5M = CFG.PUMP_EARLY_BREAKOUT_MIN_PRICE_PCT_5M
PUMP_EARLY_BREAKOUT_MAX_PRICE_PCT_5M = CFG.PUMP_EARLY_BREAKOUT_MAX_PRICE_PCT_5M
PUMP_EARLY_BREAKOUT_MIN_TXNS_5M = CFG.PUMP_EARLY_BREAKOUT_MIN_TXNS_5M
PUMP_EARLY_BREAKOUT_MIN_VOLUME_USD_24H = CFG.PUMP_EARLY_BREAKOUT_MIN_VOLUME_USD_24H
PUMP_EARLY_BREAKOUT_MIN_SCORE_TOTAL = CFG.PUMP_EARLY_BREAKOUT_MIN_SCORE_TOTAL
PUMP_EARLY_BREAKOUT_MIN_RANK_SCORE = CFG.PUMP_EARLY_BREAKOUT_MIN_RANK_SCORE
PUMP_EARLY_BREAKOUT_MIN_AGE_MIN = CFG.PUMP_EARLY_BREAKOUT_MIN_AGE_MIN
PUMP_EARLY_BREAKOUT_MAX_AGE_MIN = CFG.PUMP_EARLY_BREAKOUT_MAX_AGE_MIN
PUMP_EARLY_BREAKOUT_MAX_PRICE_IMPACT_PCT = CFG.PUMP_EARLY_BREAKOUT_MAX_PRICE_IMPACT_PCT
PUMP_EARLY_BREAKOUT_MAX_OPEN_PAPER = CFG.PUMP_EARLY_BREAKOUT_MAX_OPEN_PAPER
PUMP_EARLY_BREAKOUT_MAX_OPEN_LIVE_CANARY = CFG.PUMP_EARLY_BREAKOUT_MAX_OPEN_LIVE_CANARY
PUMP_EARLY_BREAKOUT_HEALTH_ISOLATED = CFG.PUMP_EARLY_BREAKOUT_HEALTH_ISOLATED
PUMPSWAP_PRIME_STRICT_ENABLED = CFG.PUMPSWAP_PRIME_STRICT_ENABLED
PUMPSWAP_PRIME_MIN_TXNS_5M = CFG.PUMPSWAP_PRIME_MIN_TXNS_5M
PUMPSWAP_PRIME_MIN_LIQUIDITY_USD = CFG.PUMPSWAP_PRIME_MIN_LIQUIDITY_USD
PUMPSWAP_PRIME_REQUIRE_REAL_LIQUIDITY = CFG.PUMPSWAP_PRIME_REQUIRE_REAL_LIQUIDITY
PUMPSWAP_PRIME_REQUIRE_ROUTE = CFG.PUMPSWAP_PRIME_REQUIRE_ROUTE
PUMPSWAP_PRIME_MAX_PRICE_IMPACT_PCT = CFG.PUMPSWAP_PRIME_MAX_PRICE_IMPACT_PCT
PUMPSWAP_PRIME_SHADOW_IF_NOT_STRICT = CFG.PUMPSWAP_PRIME_SHADOW_IF_NOT_STRICT
PUMPSWAP_REBOUND_PRIME_ENABLED = CFG.PUMPSWAP_REBOUND_PRIME_ENABLED
PUMPSWAP_REBOUND_PRIME_MAX_PRICE5M = CFG.PUMPSWAP_REBOUND_PRIME_MAX_PRICE5M
PUMPSWAP_REBOUND_PRIME_MIN_TXNS_5M = CFG.PUMPSWAP_REBOUND_PRIME_MIN_TXNS_5M
PUMPSWAP_REBOUND_PRIME_MIN_LIQUIDITY_USD = CFG.PUMPSWAP_REBOUND_PRIME_MIN_LIQUIDITY_USD
PUMPSWAP_REBOUND_PRIME_MIN_MCAP_USD = CFG.PUMPSWAP_REBOUND_PRIME_MIN_MCAP_USD
PUMPSWAP_REBOUND_PRIME_MAX_MCAP_USD = CFG.PUMPSWAP_REBOUND_PRIME_MAX_MCAP_USD
PUMPSWAP_REBOUND_PRIME_REQUIRE_REAL_LIQUIDITY = CFG.PUMPSWAP_REBOUND_PRIME_REQUIRE_REAL_LIQUIDITY
PUMPSWAP_REBOUND_PRIME_REQUIRE_ROUTE = CFG.PUMPSWAP_REBOUND_PRIME_REQUIRE_ROUTE
PUMPSWAP_REBOUND_PRIME_MAX_PRICE_IMPACT_PCT = CFG.PUMPSWAP_REBOUND_PRIME_MAX_PRICE_IMPACT_PCT
PUMPSWAP_REBOUND_PRIME_REQUIRE_CONFIRMATION = CFG.PUMPSWAP_REBOUND_PRIME_REQUIRE_CONFIRMATION
PUMPSWAP_REBOUND_CONFIRMATION_MIN_RECOVERY_PCT = CFG.PUMPSWAP_REBOUND_CONFIRMATION_MIN_RECOVERY_PCT
PUMPSWAP_REBOUND_CONFIRMATION_HARD_RECOVERY_PCT = CFG.PUMPSWAP_REBOUND_CONFIRMATION_HARD_RECOVERY_PCT
PUMPSWAP_REBOUND_CONFIRMATION_MIN_PRE_ENTRY_PEAK_PCT = CFG.PUMPSWAP_REBOUND_CONFIRMATION_MIN_PRE_ENTRY_PEAK_PCT
PUMPSWAP_REBOUND_CONFIRMATION_HARD_PRE_ENTRY_PEAK_PCT = CFG.PUMPSWAP_REBOUND_CONFIRMATION_HARD_PRE_ENTRY_PEAK_PCT
SNIPER_RESEARCH_SUBPROFILES_ENABLED = CFG.SNIPER_RESEARCH_SUBPROFILES_ENABLED
SNIPER_RESEARCH_MOMENTUM_IGNITION_ENABLED = CFG.SNIPER_RESEARCH_MOMENTUM_IGNITION_ENABLED
SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M = CFG.SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M
SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M = CFG.SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M
SNIPER_RESEARCH_MOMENTUM_MIN_LIQUIDITY_USD = CFG.SNIPER_RESEARCH_MOMENTUM_MIN_LIQUIDITY_USD
SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M = CFG.SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M
SNIPER_RESEARCH_MOMENTUM_MAX_TXNS_5M = CFG.SNIPER_RESEARCH_MOMENTUM_MAX_TXNS_5M
SNIPER_RESEARCH_MOMENTUM_MIN_MCAP_USD = CFG.SNIPER_RESEARCH_MOMENTUM_MIN_MCAP_USD
SNIPER_RESEARCH_MOMENTUM_MAX_MCAP_USD = CFG.SNIPER_RESEARCH_MOMENTUM_MAX_MCAP_USD
SNIPER_RESEARCH_MOMENTUM_MAX_TOP10_SHARE_PCT = CFG.SNIPER_RESEARCH_MOMENTUM_MAX_TOP10_SHARE_PCT
SNIPER_RESEARCH_MOMENTUM_ALLOW_TREND_MISSING_IF_STRONG = CFG.SNIPER_RESEARCH_MOMENTUM_ALLOW_TREND_MISSING_IF_STRONG
SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_TXNS_5M = CFG.SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_TXNS_5M
SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_RANK = CFG.SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_RANK
SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_LIQUIDITY = CFG.SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_LIQUIDITY
SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_VOLUME_24H = CFG.SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_VOLUME_24H
SNIPER_RESEARCH_DEEP_REVERSAL_ENABLED = CFG.SNIPER_RESEARCH_DEEP_REVERSAL_ENABLED
SNIPER_RESEARCH_DEEP_REVERSAL_MIN_PRICE5M = CFG.SNIPER_RESEARCH_DEEP_REVERSAL_MIN_PRICE5M
SNIPER_RESEARCH_DEEP_REVERSAL_MAX_PRICE5M = CFG.SNIPER_RESEARCH_DEEP_REVERSAL_MAX_PRICE5M
SNIPER_RESEARCH_DEEP_REVERSAL_MIN_TXNS_5M = CFG.SNIPER_RESEARCH_DEEP_REVERSAL_MIN_TXNS_5M
SNIPER_RESEARCH_DEEP_REVERSAL_MAX_MCAP_USD = CFG.SNIPER_RESEARCH_DEEP_REVERSAL_MAX_MCAP_USD
SNIPER_RESEARCH_DEEP_REVERSAL_TAKE_PROFIT_PCT = CFG.SNIPER_RESEARCH_DEEP_REVERSAL_TAKE_PROFIT_PCT
SNIPER_RESEARCH_DEEP_REVERSAL_STOP_LOSS_PCT = CFG.SNIPER_RESEARCH_DEEP_REVERSAL_STOP_LOSS_PCT
SNIPER_RESEARCH_DEEP_REVERSAL_TRAILING_PCT = CFG.SNIPER_RESEARCH_DEEP_REVERSAL_TRAILING_PCT
SNIPER_RESEARCH_DEEP_REVERSAL_TIME_STOP_MIN = CFG.SNIPER_RESEARCH_DEEP_REVERSAL_TIME_STOP_MIN
SNIPER_RESEARCH_DEEP_REVERSAL_TIME_STOP_MAX_PNL_PCT = CFG.SNIPER_RESEARCH_DEEP_REVERSAL_TIME_STOP_MAX_PNL_PCT
SNIPER_RESEARCH_DEEP_REVERSAL_TIME_STOP_MIN_PEAK_PCT = CFG.SNIPER_RESEARCH_DEEP_REVERSAL_TIME_STOP_MIN_PEAK_PCT
PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED = CFG.PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED
PUMP_EARLY_PROFIT_HEALTH_REBASE_CURRENT_GATE = CFG.PUMP_EARLY_PROFIT_HEALTH_REBASE_CURRENT_GATE
PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD = CFG.PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD
PUMP_EARLY_PROFIT_DEEP_NEG_PRICE5M_PCT = CFG.PUMP_EARLY_PROFIT_DEEP_NEG_PRICE5M_PCT
PUMP_EARLY_PROFIT_DEEP_NEG_MIN_TXNS_5M = CFG.PUMP_EARLY_PROFIT_DEEP_NEG_MIN_TXNS_5M
PUMP_EARLY_PROFIT_DEEP_NEG_MIN_VOLUME_USD_24H = CFG.PUMP_EARLY_PROFIT_DEEP_NEG_MIN_VOLUME_USD_24H
PUMP_EARLY_PROFIT_EXTREME_PRICE5M_PCT = CFG.PUMP_EARLY_PROFIT_EXTREME_PRICE5M_PCT
PUMP_EARLY_PROFIT_EXTREME_PRICE5M_MIN_MCAP_USD = CFG.PUMP_EARLY_PROFIT_EXTREME_PRICE5M_MIN_MCAP_USD
PUMP_EARLY_PROFIT_DEAD_VOLUME_MIN_USD_24H = CFG.PUMP_EARLY_PROFIT_DEAD_VOLUME_MIN_USD_24H
PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_USD_24H = CFG.PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_USD_24H
PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_TXNS_5M = CFG.PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_TXNS_5M
PUMP_EARLY_PROFIT_HOT_PRICE5M_MIN_PCT = CFG.PUMP_EARLY_PROFIT_HOT_PRICE5M_MIN_PCT
PUMP_EARLY_PROFIT_HOT_PRICE5M_MAX_PCT = CFG.PUMP_EARLY_PROFIT_HOT_PRICE5M_MAX_PCT
PUMP_EARLY_PROFIT_HOT_MCAP_MIN_USD = CFG.PUMP_EARLY_PROFIT_HOT_MCAP_MIN_USD
PUMP_EARLY_PROFIT_HOT_MIN_LIQUIDITY_USD = CFG.PUMP_EARLY_PROFIT_HOT_MIN_LIQUIDITY_USD
PUMP_EARLY_PROFIT_HOT_MIN_TXNS_5M = CFG.PUMP_EARLY_PROFIT_HOT_MIN_TXNS_5M
PUMP_EARLY_PROFIT_HOT_MIN_VOLUME_USD_24H = CFG.PUMP_EARLY_PROFIT_HOT_MIN_VOLUME_USD_24H
PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H = CFG.PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H
PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_TXNS_5M = CFG.PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_TXNS_5M
PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_PRICE5M_PCT = CFG.PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_PRICE5M_PCT
PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_TXNS_5M = CFG.PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_TXNS_5M
PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_VOLUME_USD_24H = CFG.PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_VOLUME_USD_24H
PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT = CFG.PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT
PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT = CFG.PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT
PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD = CFG.PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD
PUMP_EARLY_PROFIT_PNL_GUARD_ENABLED = CFG.PUMP_EARLY_PROFIT_PNL_GUARD_ENABLED
PUMP_EARLY_PROFIT_PNL_GUARD_JACKPOT_PRICE5M_MIN = CFG.PUMP_EARLY_PROFIT_PNL_GUARD_JACKPOT_PRICE5M_MIN
PUMP_EARLY_PROFIT_PNL_GUARD_50K_100K_WEAK_PRICE5M_MAX = CFG.PUMP_EARLY_PROFIT_PNL_GUARD_50K_100K_WEAK_PRICE5M_MAX
PUMP_EARLY_PROFIT_PNL_GUARD_50K_100K_WEAK_MIN_TXNS_5M = CFG.PUMP_EARLY_PROFIT_PNL_GUARD_50K_100K_WEAK_MIN_TXNS_5M
PUMP_EARLY_PROFIT_PNL_GUARD_LOCAL_TOP_MIN_MCAP_USD = CFG.PUMP_EARLY_PROFIT_PNL_GUARD_LOCAL_TOP_MIN_MCAP_USD
PUMP_EARLY_PROFIT_PNL_GUARD_MID_MOMENTUM_MIN_MCAP_USD = CFG.PUMP_EARLY_PROFIT_PNL_GUARD_MID_MOMENTUM_MIN_MCAP_USD
PUMP_EARLY_PROFIT_MAX_OPEN_PAPER = CFG.PUMP_EARLY_PROFIT_MAX_OPEN_PAPER
PUMP_EARLY_PROFIT_MAX_OPEN_LIVE_CANARY = CFG.PUMP_EARLY_PROFIT_MAX_OPEN_LIVE_CANARY
PUMP_EARLY_PROFIT_RUNNER_BROAD_LOCK_FLOOR_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_BROAD_LOCK_FLOOR_PCT
PUMP_EARLY_PROFIT_RUNNER_BROAD_PARTIAL_FRACTION = CFG.PUMP_EARLY_PROFIT_RUNNER_BROAD_PARTIAL_FRACTION
PUMP_EARLY_PROFIT_RUNNER_BROAD_MAX_GIVEBACK_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_BROAD_MAX_GIVEBACK_PCT
PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_LOCK_FLOOR_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_LOCK_FLOOR_PCT
PUMP_EARLY_PROFIT_RUNNER_PRIME_PARTIAL_FRACTION = CFG.PUMP_EARLY_PROFIT_RUNNER_PRIME_PARTIAL_FRACTION
PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_MAX_GIVEBACK_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_MAX_GIVEBACK_PCT
PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_LOCK_FLOOR_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_LOCK_FLOOR_PCT
PUMP_EARLY_PROFIT_RUNNER_METEOR_PARTIAL_FRACTION = CFG.PUMP_EARLY_PROFIT_RUNNER_METEOR_PARTIAL_FRACTION
PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_MAX_GIVEBACK_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_MAX_GIVEBACK_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_ENABLED = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_ENABLED
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_LIQUIDITY_USD = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_LIQUIDITY_USD
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_MCAP_USD = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_MCAP_USD
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MAX_MCAP_USD = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MAX_MCAP_USD
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_PRICE5M_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_PRICE5M_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MAX_PRICE5M_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MAX_PRICE5M_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_TXNS_5M = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_TXNS_5M
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_RANK_SCORE = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MIN_RANK_SCORE
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_PARTIAL_FRACTION = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_PARTIAL_FRACTION
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_BASE_LOCK_FLOOR_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_BASE_LOCK_FLOOR_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_BASE_MAX_GIVEBACK_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_BASE_MAX_GIVEBACK_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_PEAK_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_PEAK_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_LOCK_FLOOR_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_LOCK_FLOOR_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_MAX_GIVEBACK_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP1_MAX_GIVEBACK_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_PEAK_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_PEAK_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_LOCK_FLOOR_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_LOCK_FLOOR_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_MAX_GIVEBACK_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP2_MAX_GIVEBACK_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_PEAK_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_PEAK_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_LOCK_FLOOR_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_LOCK_FLOOR_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_MAX_GIVEBACK_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP3_MAX_GIVEBACK_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_PEAK_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_PEAK_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_LOCK_FLOOR_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_LOCK_FLOOR_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_MAX_GIVEBACK_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_STEP4_MAX_GIVEBACK_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP1_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP1_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP1_FRACTION = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP1_FRACTION
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP2_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP2_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP2_FRACTION = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP2_FRACTION
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP3_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP3_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP3_FRACTION = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP3_FRACTION
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP4_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP4_PCT
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP4_FRACTION = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_TP4_FRACTION
PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MOONBAG_FRACTION = CFG.PUMP_EARLY_PROFIT_RUNNER_JACKPOT_MOONBAG_FRACTION
GREEN_SNIPER_MOONSHOT_TP1_PCT = CFG.GREEN_SNIPER_MOONSHOT_TP1_PCT
GREEN_SNIPER_MOONSHOT_TP1_FRACTION = CFG.GREEN_SNIPER_MOONSHOT_TP1_FRACTION
GREEN_SNIPER_MOONSHOT_TP2_PCT = CFG.GREEN_SNIPER_MOONSHOT_TP2_PCT
GREEN_SNIPER_MOONSHOT_TP2_FRACTION = CFG.GREEN_SNIPER_MOONSHOT_TP2_FRACTION
GREEN_SNIPER_MOONSHOT_TP3_PCT = CFG.GREEN_SNIPER_MOONSHOT_TP3_PCT
GREEN_SNIPER_MOONSHOT_TP3_FRACTION = CFG.GREEN_SNIPER_MOONSHOT_TP3_FRACTION
GREEN_SNIPER_MOONSHOT_TP4_PCT = CFG.GREEN_SNIPER_MOONSHOT_TP4_PCT
GREEN_SNIPER_MOONSHOT_TP4_FRACTION = CFG.GREEN_SNIPER_MOONSHOT_TP4_FRACTION
GREEN_SNIPER_MOONSHOT_MOONBAG_FRACTION = CFG.GREEN_SNIPER_MOONSHOT_MOONBAG_FRACTION
MOONSHOT_MICRO_LOTTERY_ENABLED = CFG.MOONSHOT_MICRO_LOTTERY_ENABLED
MOONSHOT_MICRO_LOTTERY_PAPER_ENABLED = CFG.MOONSHOT_MICRO_LOTTERY_PAPER_ENABLED
MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED = CFG.MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED
MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL = CFG.MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL
MOONSHOT_MICRO_LOTTERY_MAX_OPEN = CFG.MOONSHOT_MICRO_LOTTERY_MAX_OPEN
MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS = CFG.MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS
MOONSHOT_MICRO_LOTTERY_MAX_AGE_MIN = CFG.MOONSHOT_MICRO_LOTTERY_MAX_AGE_MIN
MOONSHOT_MICRO_LOTTERY_MIN_TXNS_5M = CFG.MOONSHOT_MICRO_LOTTERY_MIN_TXNS_5M
MOONSHOT_MICRO_LOTTERY_MAX_MCAP_USD = CFG.MOONSHOT_MICRO_LOTTERY_MAX_MCAP_USD
MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M = CFG.MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M
MOONSHOT_MICRO_LOTTERY_TP1_PCT = CFG.MOONSHOT_MICRO_LOTTERY_TP1_PCT
MOONSHOT_MICRO_LOTTERY_TP1_FRACTION = CFG.MOONSHOT_MICRO_LOTTERY_TP1_FRACTION
MOONSHOT_MICRO_LOTTERY_TP2_PCT = CFG.MOONSHOT_MICRO_LOTTERY_TP2_PCT
MOONSHOT_MICRO_LOTTERY_TP2_FRACTION = CFG.MOONSHOT_MICRO_LOTTERY_TP2_FRACTION
MOONSHOT_MICRO_LOTTERY_TP3_PCT = CFG.MOONSHOT_MICRO_LOTTERY_TP3_PCT
MOONSHOT_MICRO_LOTTERY_TP3_FRACTION = CFG.MOONSHOT_MICRO_LOTTERY_TP3_FRACTION
MOONSHOT_MICRO_LOTTERY_TP4_PCT = CFG.MOONSHOT_MICRO_LOTTERY_TP4_PCT
MOONSHOT_MICRO_LOTTERY_TP4_FRACTION = CFG.MOONSHOT_MICRO_LOTTERY_TP4_FRACTION
MOONSHOT_MICRO_LOTTERY_MOONBAG_FRACTION = CFG.MOONSHOT_MICRO_LOTTERY_MOONBAG_FRACTION
MOONSHOT_MICRO_LOTTERY_TIME_STOP_MIN = CFG.MOONSHOT_MICRO_LOTTERY_TIME_STOP_MIN
MOONSHOT_MICRO_LOTTERY_TIME_STOP_MAX_PNL_PCT = CFG.MOONSHOT_MICRO_LOTTERY_TIME_STOP_MAX_PNL_PCT
MOONSHOT_MICRO_LOTTERY_HARD_STOP_PCT = CFG.MOONSHOT_MICRO_LOTTERY_HARD_STOP_PCT
MOONSHOT_MICRO_LOTTERY_NO_EXPANSION_EXIT_S = CFG.MOONSHOT_MICRO_LOTTERY_NO_EXPANSION_EXIT_S
GREEN_SNIPER_STEP4_PEAK_PCT = CFG.GREEN_SNIPER_STEP4_PEAK_PCT
GREEN_SNIPER_STEP4_LOCK_FLOOR_PCT = CFG.GREEN_SNIPER_STEP4_LOCK_FLOOR_PCT
GREEN_SNIPER_STEP4_MAX_GIVEBACK_PCT = CFG.GREEN_SNIPER_STEP4_MAX_GIVEBACK_PCT
GREEN_SNIPER_STEP5_PEAK_PCT = CFG.GREEN_SNIPER_STEP5_PEAK_PCT
GREEN_SNIPER_STEP5_LOCK_FLOOR_PCT = CFG.GREEN_SNIPER_STEP5_LOCK_FLOOR_PCT
GREEN_SNIPER_STEP5_MAX_GIVEBACK_PCT = CFG.GREEN_SNIPER_STEP5_MAX_GIVEBACK_PCT
PUMP_EARLY_SUBLANE_HEALTH_ENABLED = CFG.PUMP_EARLY_SUBLANE_HEALTH_ENABLED
PUMP_EARLY_SUBLANE_HEALTH_WINDOW_TRADES = CFG.PUMP_EARLY_SUBLANE_HEALTH_WINDOW_TRADES
PUMP_EARLY_SUBLANE_HEALTH_MIN_TRADES = CFG.PUMP_EARLY_SUBLANE_HEALTH_MIN_TRADES
PUMP_EARLY_SUBLANE_HEALTH_MAX_AVG_PNL_PCT = CFG.PUMP_EARLY_SUBLANE_HEALTH_MAX_AVG_PNL_PCT
PUMP_EARLY_SUBLANE_HEALTH_MAX_SEVERE_EXITS = CFG.PUMP_EARLY_SUBLANE_HEALTH_MAX_SEVERE_EXITS
PUMP_EARLY_SUBLANE_HEALTH_MAX_LIQ_CRUSH_EXITS = CFG.PUMP_EARLY_SUBLANE_HEALTH_MAX_LIQ_CRUSH_EXITS
PUMP_EARLY_SUBLANE_HEALTH_MIN_CANARY_TRADES = CFG.PUMP_EARLY_SUBLANE_HEALTH_MIN_CANARY_TRADES
PUMP_EARLY_SUBLANE_HEALTH_MAX_CANARY_AVG_PNL_PCT = CFG.PUMP_EARLY_SUBLANE_HEALTH_MAX_CANARY_AVG_PNL_PCT
PUMP_EARLY_SUBLANE_HEALTH_MAX_CANARY_SEVERE_EXITS = CFG.PUMP_EARLY_SUBLANE_HEALTH_MAX_CANARY_SEVERE_EXITS
PUMP_EARLY_SUBLANE_HEALTH_MAX_CANARY_LIQ_CRUSH_EXITS = CFG.PUMP_EARLY_SUBLANE_HEALTH_MAX_CANARY_LIQ_CRUSH_EXITS
PUMP_EARLY_PROFIT_RUNNER_METEOR_MOMENTUM_PRICE5M_PCT = CFG.PUMP_EARLY_PROFIT_RUNNER_METEOR_MOMENTUM_PRICE5M_PCT
PUMP_EARLY_PROFIT_RUNNER_METEOR_MOMENTUM_MIN_TXNS_5M = CFG.PUMP_EARLY_PROFIT_RUNNER_METEOR_MOMENTUM_MIN_TXNS_5M
PUMP_EARLY_RESEARCH_ALLOW_PROXY = CFG.PUMP_EARLY_RESEARCH_ALLOW_PROXY
PAPER_PNL_STRICT_HEALTH = CFG.PAPER_PNL_STRICT_HEALTH
LIVE_RANK_SCORE_FALLBACK_MIN = CFG.LIVE_RANK_SCORE_FALLBACK_MIN

# Re-queues
INCOMPLETE_RETRIES = CFG.INCOMPLETE_RETRIES
MAX_RETRIES = CFG.MAX_RETRIES

# Trading amounts / balances
TRADE_AMOUNT_SOL = CFG.TRADE_AMOUNT_SOL
GAS_RESERVE_SOL = CFG.GAS_RESERVE_SOL
MIN_BUY_SOL = CFG.MIN_BUY_SOL
MIN_SOL_BALANCE = CFG.MIN_SOL_BALANCE

# DB
SQLITE_DB = CFG.SQLITE_DB
DB_URI = f"sqlite+aiosqlite:///{pathlib.Path(SQLITE_DB).expanduser().resolve()}"

# Riesgo/Exits
TAKE_PROFIT_PCT = CFG.TAKE_PROFIT_PCT
STOP_LOSS_PCT = CFG.STOP_LOSS_PCT
TRAILING_PCT = CFG.TRAILING_PCT
MAX_HOLDING_H = CFG.MAX_HOLDING_H
MAX_HARD_HOLD_H = CFG.MAX_HARD_HOLD_H
WIN_PCT = CFG.WIN_PCT
ML_POSITIVE_PNL_PCT = CFG.ML_POSITIVE_PNL_PCT
ML_POSITIVE_PNL_RATIO = CFG.ML_POSITIVE_PNL_RATIO
LABEL_GRACE_H = CFG.LABEL_GRACE_H

# Horarios modernos
TRADING_HOURS = CFG.TRADING_HOURS
TRADING_HOURS_EXTRA = CFG.TRADING_HOURS_EXTRA
USE_EXTRA_HOURS = CFG.USE_EXTRA_HOURS
BLOCK_HOURS = CFG.BLOCK_HOURS

# Zona horaria local (objeto ZoneInfo)
LOCAL_TZ_NAME = CFG.LOCAL_TZ_NAME
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:
    LOCAL_TZ = ZoneInfo("Europe/Madrid")

# Ventanas legacy (compat)
TRADING_WINDOWS = CFG.TRADING_WINDOWS
TRADING_WINDOWS_PARSED: tuple[tuple[int, int], ...] = _parse_windows(TRADING_WINDOWS)
TRADING_STRICT = CFG.TRADING_STRICT

# Compra / requisitos
REQUIRE_JUPITER_FOR_BUY = CFG.REQUIRE_JUPITER_FOR_BUY
DEX_WHITELIST = CFG.DEX_WHITELIST
REQUIRE_POOL_INITIALIZED = CFG.REQUIRE_POOL_INITIALIZED
BUY_RATE_LIMIT_N = CFG.BUY_RATE_LIMIT_N
BUY_RATE_LIMIT_WINDOW_S = CFG.BUY_RATE_LIMIT_WINDOW_S

# Monitor / shadow-sim
FORCE_JUP_IN_MONITOR = CFG.FORCE_JUP_IN_MONITOR
REAL_SHADOW_SIM = CFG.REAL_SHADOW_SIM

# Señales de salida mejoradas
EARLY_DROP_KILL_PCT = CFG.EARLY_DROP_KILL_PCT
EARLY_DROP_WINDOW_MIN = CFG.EARLY_DROP_WINDOW_MIN
LIQ_CRUSH_DROP_PCT = CFG.LIQ_CRUSH_DROP_PCT
LIQ_CRUSH_WINDOW_MIN = CFG.LIQ_CRUSH_WINDOW_MIN

# IA thresholds + entreno
AI_THRESHOLD = CFG.AI_THRESHOLD
AI_TH = AI_THRESHOLD  # alias compat
AI_THRESHOLD_FILE = CFG.AI_THRESHOLD_FILE
BUY_SOFT_SCORE_MIN = CFG.BUY_SOFT_SCORE_MIN
TRAIN_FORWARD_HOLDOUT_DAYS = CFG.TRAIN_FORWARD_HOLDOUT_DAYS
TRAIN_FORWARD_HOLDOUT_PCT = CFG.TRAIN_FORWARD_HOLDOUT_PCT
TRAINING_WINDOW_DAYS = CFG.TRAINING_WINDOW_DAYS
MIN_THRESHOLD_CHANGE = CFG.MIN_THRESHOLD_CHANGE
PRECISION_AT_K_PCT = CFG.PRECISION_AT_K_PCT
ML_GATE_MODE = CFG.ML_GATE_MODE
ML_SHADOW_CANDIDATE_MODEL_FALLBACK_ENABLED = CFG.ML_SHADOW_CANDIDATE_MODEL_FALLBACK_ENABLED
ML_MIN_DATASET_ROWS = CFG.ML_MIN_DATASET_ROWS
ML_MIN_POSITIVES = CFG.ML_MIN_POSITIVES
ML_MIN_UNIQUE_TOKENS = CFG.ML_MIN_UNIQUE_TOKENS
ML_MIN_REALIZED_RETURN_ROWS = CFG.ML_MIN_REALIZED_RETURN_ROWS
ML_MIN_HOLDOUT_ROWS = CFG.ML_MIN_HOLDOUT_ROWS
ML_MIN_HOLDOUT_POSITIVES = CFG.ML_MIN_HOLDOUT_POSITIVES
ML_MIN_NON_CONSTANT_FEATURES = CFG.ML_MIN_NON_CONSTANT_FEATURES
ML_TUNE_OBJECTIVE = CFG.ML_TUNE_OBJECTIVE
ML_TUNE_PRECISION_FLOOR = CFG.ML_TUNE_PRECISION_FLOOR
ML_TUNE_MIN_SELECTED = CFG.ML_TUNE_MIN_SELECTED
ML_TUNE_MIN_REALIZED_SELECTED = CFG.ML_TUNE_MIN_REALIZED_SELECTED
ML_SELECTION_MIN_DELTA = CFG.ML_SELECTION_MIN_DELTA
ML_TRAIN_ENTRY_LANE_ALLOWLIST = CFG.ML_TRAIN_ENTRY_LANE_ALLOWLIST
ML_TRAIN_ALLOW_MISSING_ENTRY_LANE = CFG.ML_TRAIN_ALLOW_MISSING_ENTRY_LANE
ML_TRAIN_DEX_ALLOWLIST = CFG.ML_TRAIN_DEX_ALLOWLIST
ML_BOOTSTRAP_RESEARCH_SHADOW_ENABLED = CFG.ML_BOOTSTRAP_RESEARCH_SHADOW_ENABLED
ML_BOOTSTRAP_ONLY_WHEN_MODEL_MISSING = CFG.ML_BOOTSTRAP_ONLY_WHEN_MODEL_MISSING
ML_BOOTSTRAP_ENTRY_LANE_ALLOWLIST = CFG.ML_BOOTSTRAP_ENTRY_LANE_ALLOWLIST
ML_BOOTSTRAP_DEX_ALLOWLIST = CFG.ML_BOOTSTRAP_DEX_ALLOWLIST
PUMP_EARLY_SHADOW_RECOVERY_ENABLED = CFG.PUMP_EARLY_SHADOW_RECOVERY_ENABLED
PUMP_EARLY_SHADOW_RECOVERY_WINDOW = CFG.PUMP_EARLY_SHADOW_RECOVERY_WINDOW
PUMP_EARLY_SHADOW_RECOVERY_MIN_TRADES = CFG.PUMP_EARLY_SHADOW_RECOVERY_MIN_TRADES
PUMP_EARLY_SHADOW_RECOVERY_MIN_AVG_PNL_PCT = CFG.PUMP_EARLY_SHADOW_RECOVERY_MIN_AVG_PNL_PCT
PUMP_EARLY_SHADOW_RECOVERY_MIN_WIN_RATE_PCT = CFG.PUMP_EARLY_SHADOW_RECOVERY_MIN_WIN_RATE_PCT
PUMP_EARLY_SHADOW_RECOVERY_MAX_SEVERE_EXITS = CFG.PUMP_EARLY_SHADOW_RECOVERY_MAX_SEVERE_EXITS
PUMP_EARLY_SHADOW_RECOVERY_MAX_LIQ_CRUSH = CFG.PUMP_EARLY_SHADOW_RECOVERY_MAX_LIQ_CRUSH
PUMP_EARLY_SHADOW_RECOVERY_MAX_CONSECUTIVE_LOSSES = CFG.PUMP_EARLY_SHADOW_RECOVERY_MAX_CONSECUTIVE_LOSSES
PUMP_EARLY_SHADOW_RECOVERY_MAX_AGE_H = CFG.PUMP_EARLY_SHADOW_RECOVERY_MAX_AGE_H
POST_PARTIAL_PROTECTION_ENABLED = CFG.POST_PARTIAL_PROTECTION_ENABLED
POST_PARTIAL_PROTECTION_PAPER_ENABLED = CFG.POST_PARTIAL_PROTECTION_PAPER_ENABLED
POST_PARTIAL_PROTECTION_LIVE_ENABLED = CFG.POST_PARTIAL_PROTECTION_LIVE_ENABLED
POST_PARTIAL_PROTECTION_EXECUTION_ENABLED = CFG.POST_PARTIAL_PROTECTION_EXECUTION_ENABLED
POST_PARTIAL_LOCK_FLOOR_ENABLED = CFG.POST_PARTIAL_LOCK_FLOOR_ENABLED
POST_PARTIAL_LOCK_FLOOR_PCT = CFG.POST_PARTIAL_LOCK_FLOOR_PCT
POST_PARTIAL_MAX_GIVEBACK_PCT = CFG.POST_PARTIAL_MAX_GIVEBACK_PCT
POST_PARTIAL_MIN_PEAK_PCT = CFG.POST_PARTIAL_MIN_PEAK_PCT
POST_PARTIAL_EXPERIMENT_ENABLED = CFG.POST_PARTIAL_EXPERIMENT_ENABLED
POST_PARTIAL_EXPERIMENT_SHADOW_ONLY = CFG.POST_PARTIAL_EXPERIMENT_SHADOW_ONLY
POST_PARTIAL_EXPERIMENT_MODE = CFG.POST_PARTIAL_EXPERIMENT_MODE
POST_PARTIAL_EXPERIMENT_REGIME = CFG.POST_PARTIAL_EXPERIMENT_REGIME
POST_PARTIAL_EXPERIMENT_LOCK_FLOOR_PCT = CFG.POST_PARTIAL_EXPERIMENT_LOCK_FLOOR_PCT
POST_PARTIAL_EXPERIMENT_MAX_GIVEBACK_PCT = CFG.POST_PARTIAL_EXPERIMENT_MAX_GIVEBACK_PCT
POST_PARTIAL_EXPERIMENT_MIN_NEW_CLOSES = CFG.POST_PARTIAL_EXPERIMENT_MIN_NEW_CLOSES
POST_PARTIAL_EXPERIMENT_LOCKED_ML_THRESHOLD = CFG.POST_PARTIAL_EXPERIMENT_LOCKED_ML_THRESHOLD
BIRD_RUNNER_MULTI_PARTIAL_ENABLED = CFG.BIRD_RUNNER_MULTI_PARTIAL_ENABLED
BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED = CFG.BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED
BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED = CFG.BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED
BIRD_TP1_PCT = CFG.BIRD_TP1_PCT
BIRD_TP1_FRACTION = CFG.BIRD_TP1_FRACTION
BIRD_TP2_PCT = CFG.BIRD_TP2_PCT
BIRD_TP2_FRACTION = CFG.BIRD_TP2_FRACTION
BIRD_TP3_PCT = CFG.BIRD_TP3_PCT
BIRD_TP3_FRACTION = CFG.BIRD_TP3_FRACTION
BIRD_TP4_PCT = CFG.BIRD_TP4_PCT
BIRD_TP4_FRACTION = CFG.BIRD_TP4_FRACTION
BIRD_TP5_PCT = CFG.BIRD_TP5_PCT
BIRD_TP5_FRACTION = CFG.BIRD_TP5_FRACTION
BIRD_TP6_PCT = CFG.BIRD_TP6_PCT
BIRD_TP6_FRACTION = CFG.BIRD_TP6_FRACTION
BIRD_MOONBAG_FRACTION = CFG.BIRD_MOONBAG_FRACTION
DYNAMIC_RUNNER_FLOOR_ENABLED = CFG.DYNAMIC_RUNNER_FLOOR_ENABLED
RUNNER_FLOOR_PEAK_100 = CFG.RUNNER_FLOOR_PEAK_100
RUNNER_FLOOR_PEAK_300 = CFG.RUNNER_FLOOR_PEAK_300
RUNNER_FLOOR_PEAK_700 = CFG.RUNNER_FLOOR_PEAK_700
RUNNER_FLOOR_PEAK_1000 = CFG.RUNNER_FLOOR_PEAK_1000
RUNNER_FLOOR_PEAK_2000 = CFG.RUNNER_FLOOR_PEAK_2000
RUNNER_GIVEBACK_EMERGENCY_ENABLED = CFG.RUNNER_GIVEBACK_EMERGENCY_ENABLED
RUNNER_GIVEBACK_EMERGENCY_PAPER_ENABLED = CFG.RUNNER_GIVEBACK_EMERGENCY_PAPER_ENABLED
RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED = CFG.RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED
RUNNER_GIVEBACK_PEAK_100_MAX_GIVEBACK = CFG.RUNNER_GIVEBACK_PEAK_100_MAX_GIVEBACK
RUNNER_GIVEBACK_PEAK_300_MAX_GIVEBACK = CFG.RUNNER_GIVEBACK_PEAK_300_MAX_GIVEBACK
RUNNER_GIVEBACK_PEAK_700_MAX_GIVEBACK = CFG.RUNNER_GIVEBACK_PEAK_700_MAX_GIVEBACK
RUNNER_GIVEBACK_PEAK_1000_MAX_GIVEBACK = CFG.RUNNER_GIVEBACK_PEAK_1000_MAX_GIVEBACK
RUNNER_GIVEBACK_PEAK_2000_MAX_GIVEBACK = CFG.RUNNER_GIVEBACK_PEAK_2000_MAX_GIVEBACK
RUNNER_GIVEBACK_CLOSE_REMAINING = CFG.RUNNER_GIVEBACK_CLOSE_REMAINING
RUNNER_TURBO_MONITOR_ENABLED = CFG.RUNNER_TURBO_MONITOR_ENABLED
RUNNER_TURBO_PEAK_PCT = CFG.RUNNER_TURBO_PEAK_PCT
RUNNER_TURBO_INTERVAL_S = CFG.RUNNER_TURBO_INTERVAL_S
RUNNER_TURBO_MAX_DURATION_MIN = CFG.RUNNER_TURBO_MAX_DURATION_MIN
RUNNER_TURBO_PAPER_ONLY = CFG.RUNNER_TURBO_PAPER_ONLY

# Estrategia avanzada
REVIVAL_LIQ_USD = CFG.REVIVAL_LIQ_USD
REVIVAL_VOL1H_USD = CFG.REVIVAL_VOL1H_USD
REVIVAL_PC_5M = CFG.REVIVAL_PC_5M
BUY_FROM_CURVE = CFG.BUY_FROM_CURVE
CURVE_BUY_RANK_MAX = CFG.CURVE_BUY_RANK_MAX
CURVE_MAX_COST = CFG.CURVE_MAX_COST

# Miscelánea
SOL_PUBLIC_KEY = CFG.SOL_PUBLIC_KEY
BANNED_CREATORS = CFG.BANNED_CREATORS
LOG_LEVEL = CFG.LOG_LEVEL
LOG_PATH = CFG.LOG_PATH
FEATURES_DIR = CFG.FEATURES_DIR
MODEL_PATH = CFG.MODEL_PATH

__all__ = [
    "CFG",
    # helper exports (legacy)
    "PROJECT_ROOT",
    "DB_URI",
    "LOCAL_TZ",
    "TRADING_WINDOWS_PARSED",
    # common config exports
    "MIN_AGE_MIN",
    "MIN_LIQUIDITY_USD",
    "MIN_VOL_USD_24H",
    "MIN_HOLDERS",
    "MAX_MARKET_CAP_USD",
    "AI_THRESHOLD",
    "AI_TH",
]
