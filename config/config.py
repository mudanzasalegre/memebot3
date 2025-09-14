# config/config.py – MemeBot 3
"""
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

from dotenv import load_dotenv
from zoneinfo import ZoneInfo

T = TypeVar("T", int, float)
_num_re = re.compile(r"-?\d+(?:\.\d+)?")


# ───────────────────────── helpers ──────────────────────────
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
        raw = os.getenv(key)
        if raw is not None:
            return _num_env(key, cast, default)
    return default


def _csv_tuple(raw: str, *, lower: bool = True, strip: bool = True) -> Tuple[str, ...]:
    """
    Convierte un CSV en tupla normalizada (sin entradas vacías).
    lower=True → pasa a minúsculas; strip=True → quita espacios.
    """
    if not raw:
        return tuple()
    items = []
    for part in str(raw).split(","):
        s = part.strip() if strip else part
        if not s:
            continue
        items.append(s.lower() if lower else s)
    # dedup preservando orden
    seen = set()
    out = []
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
    for chunk in raw.split(","):
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
load_dotenv(PROJECT_ROOT / ".env", override=True)


# ───────────────────────── Config dataclass ─────────────────
@dataclass(frozen=True)
class _Config:
    # ------- modo ---------------------------------------------------
    DRY_RUN: bool = os.getenv("DRY_RUN", "0") == "1"
    TRADE_AMOUNT_SOL: float = _num_env("TRADE_AMOUNT_SOL", float, 0.1)
    GAS_RESERVE_SOL: float = _num_env("GAS_RESERVE_SOL", float, 0.05)
    MIN_BUY_SOL: float = _num_env("MIN_BUY_SOL", float, 0.01)
    MIN_SOL_BALANCE: float = _num_env("MIN_SOL_BALANCE", float, 0.01)
    DEXS_TTL_NIL: int = _num_env("DEXS_TTL_NIL", int, 300)
    DEXS_TTL_OK: int = _num_env("DEXS_TTL_OK", int, 30)

    # ------- IA / ML -----------------------------------------------
    # Unificado: AI_THRESHOLD (lee AI_THRESHOLD o AI_TH; prioridad a AI_THRESHOLD)
    AI_THRESHOLD: float = _num_env_multi(["AI_THRESHOLD", "AI_TH"], float, 0.65)
    BUY_SOFT_SCORE_MIN: int = _num_env("BUY_SOFT_SCORE_MIN", int, 40)
    FEATURES_DIR: pathlib.Path = pathlib.Path(
        os.getenv("FEATURES_DIR", PROJECT_ROOT / "data" / "features")
    )
    MODEL_PATH: pathlib.Path = pathlib.Path(
        os.getenv("MODEL_PATH", PROJECT_ROOT / "ml" / "model.pkl")
    )
    AI_THRESHOLD_FILE: pathlib.Path = pathlib.Path(
        os.getenv("AI_THRESHOLD_FILE", PROJECT_ROOT / "data" / "metrics" / "recommended_threshold.json")
    )
    RETRAIN_DAY: int = _num_env("RETRAIN_DAY", int, 6)
    RETRAIN_HOUR: int = _num_env("RETRAIN_HOUR", int, 4)

    # —— nuevos de entrenamiento/validación ——
    TRAIN_FORWARD_HOLDOUT_DAYS: int = _num_env("TRAIN_FORWARD_HOLDOUT_DAYS", int, 3)
    TRAIN_FORWARD_HOLDOUT_PCT: float = _num_env("TRAIN_FORWARD_HOLDOUT_PCT", float, 0.0)  # 0 → ignorar
    TRAINING_WINDOW_DAYS: int = _num_env("TRAINING_WINDOW_DAYS", int, 28)
    MIN_THRESHOLD_CHANGE: float = _num_env("MIN_THRESHOLD_CHANGE", float, 0.01)  # 0.01 = 1 p.p.

    # ------- endpoints ---------------------------------------------
    RPC_URL: str = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    DEXSCREENER_API: str = (
        os.getenv("DEXSCREENER_API")
        or os.getenv("DEX_API_BASE")
        or "https://api.dexscreener.com"
    )

    # ------- Helius -------------------------------------------------
    HELIUS_API_KEY: str | None = os.getenv("HELIUS_API_KEY")
    HELIUS_REST_BASE: str = os.getenv(
        "HELIUS_REST_BASE",
        os.getenv("HELIUS_API_BASE", "https://api.helius.xyz"),
    )
    HELIUS_RPC_URL: str = os.getenv(
        "HELIUS_RPC_URL",
        f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        if HELIUS_API_KEY
        else "https://api.mainnet-beta.solana.com",
    )

    # ------- otros servicios ---------------------------------------
    RUGCHECK_API_BASE: str = os.getenv("RUGCHECK_API_BASE", "https://api.rugcheck.xyz/v1")
    RUGCHECK_API_KEY: str | None = os.getenv("RUGCHECK_API_KEY")
    BITQUERY_TOKEN: str | None = os.getenv("BITQUERY_TOKEN")
    PUMPFUN_PROGRAM: str | None = os.getenv("PUMPFUN_PROGRAM")

    # ------- GeckoTerminal -----------------------------------------
    USE_GECKO_TERMINAL: bool = os.getenv("USE_GECKO_TERMINAL", "true").lower() == "true"
    GECKO_API_URL: str = os.getenv("GECKO_API_URL", "https://api.geckoterminal.com/api/v2")
    GECKO_SOL_ENDPOINT: str = f"{GECKO_API_URL}/networks/solana/pools"

    # ------- Jupiter Price v3 (Lite) -------------------------------
    USE_JUPITER_PRICE: bool = os.getenv("USE_JUPITER_PRICE", "true").lower() == "true"
    JUPITER_PRICE_URL: str = os.getenv("JUPITER_PRICE_URL", "https://lite-api.jup.ag/price/v3")
    JUPITER_RPM: int = _num_env("JUPITER_RPM", int, 60)
    JUPITER_TTL_NIL_SHORT: int = _num_env("JUPITER_TTL_NIL_SHORT", int, 120)
    JUPITER_TTL_NIL_MAX: int = _num_env("JUPITER_TTL_NIL_MAX", int, 600)
    JUPITER_TTL_OK: int = _num_env("JUPITER_TTL_OK", int, 120)

    # ------- Impacto Jupiter (router opcional) ---------------------
    USE_JUPITER_IMPACT: bool = os.getenv("USE_JUPITER_IMPACT", "false").lower() == "true"
    IMPACT_PROBE_SOL: float = _num_env("IMPACT_PROBE_SOL", float, 0.05)
    IMPACT_MAX_PCT: float = _num_env("IMPACT_MAX_PCT", float, 8.0)

    # ------- filtros básicos ---------------------------------------
    MAX_AGE_DAYS: float = _num_env("MAX_AGE_DAYS", float, 2)
    MIN_AGE_MIN: float = _num_env("MIN_AGE_MIN", float, 3.0)  # ↑ default 3.0 (antes 2.0)
    MIN_HOLDERS: int = _num_env("MIN_HOLDERS", int, 10)
    MIN_LIQUIDITY_USD: float = _num_env("MIN_LIQUIDITY_USD", float, 5_000)
    MIN_VOL_USD_24H: float = _num_env("MIN_VOL_USD_24H", float, 10_000)
    MIN_MARKET_CAP_USD: float = _num_env("MIN_MARKET_CAP_USD", float, 5_000)
    MAX_MARKET_CAP_USD: float = _num_env("MAX_MARKET_CAP_USD", float, 20_000)
    MAX_24H_VOLUME: float = _num_env("MAX_24H_VOLUME", float, 1_500_000)
    MIN_SCORE_TOTAL: int = _num_env("MIN_SCORE_TOTAL", int, 50)
    MAX_ACTIVE_POSITIONS: int = _num_env("MAX_ACTIVE_POSITIONS", int, 25)
    DEXS_TXNS_5M_MIN: int = _num_env("DEXS_TXNS_5M_MIN", int, 2)  # umbral DexScreener

    # ------- control horario (.env moderno) ------------------------
    TRADING_HOURS: str = os.getenv("TRADING_HOURS", "")                 # ej. "0-2" (CEST)
    TRADING_HOURS_EXTRA: str = os.getenv("TRADING_HOURS_EXTRA", "")     # ej. "9-10"
    USE_EXTRA_HOURS: bool = os.getenv("USE_EXTRA_HOURS", "false").lower() == "true"
    LOCAL_TZ_NAME: str = os.getenv("LOCAL_TZ", "Europe/Madrid")
    BLOCK_HOURS: str = os.getenv("BLOCK_HOURS", "")                     # ej. "3,12,17-19"

    # ------- trading windows (legacy, por compatibilidad) ----------
    TRADING_WINDOWS: str = os.getenv("TRADING_WINDOWS", "13-16")
    TRADING_STRICT: bool = os.getenv("TRADING_STRICT", "true").lower() == "true"

    # ------- compra / requisitos -----------------------------------
    REQUIRE_JUPITER_FOR_BUY: bool = os.getenv("REQUIRE_JUPITER_FOR_BUY", "true").lower() == "true"
    DEX_WHITELIST: Tuple[str, ...] = _csv_tuple(
        os.getenv("DEX_WHITELIST", "raydium,orca,meteora"), lower=True, strip=True
    )
    REQUIRE_POOL_INITIALIZED: bool = os.getenv("REQUIRE_POOL_INITIALIZED", "true").lower() == "true"
    BUY_RATE_LIMIT_N: int = _num_env("BUY_RATE_LIMIT_N", int, 3)
    BUY_RATE_LIMIT_WINDOW_S: int = _num_env("BUY_RATE_LIMIT_WINDOW_S", int, 120)

    # ------- monitor / shadow-sim ----------------------------------
    FORCE_JUP_IN_MONITOR: bool = os.getenv("FORCE_JUP_IN_MONITOR", "false").lower() == "true"
    REAL_SHADOW_SIM: bool = os.getenv("REAL_SHADOW_SIM", "false").lower() == "true"

    # ------- riesgo / exits ----------------------------------------
    TAKE_PROFIT_PCT: float = _num_env("TAKE_PROFIT_PCT", float, 35)
    STOP_LOSS_PCT: float = _num_env("STOP_LOSS_PCT", float, 20)
    TRAILING_PCT: float = _num_env("TRAILING_PCT", float, 30)
    MAX_HOLDING_H: int = _num_env("MAX_HOLDING_H", int, 24)
    # Límite duro opcional si extiendes por trailing (p.ej. 4h)
    MAX_HARD_HOLD_H: int = _num_env("MAX_HARD_HOLD_H", int, 4)

    # Salidas mejoradas
    EARLY_DROP_KILL_PCT: float = _num_env("EARLY_DROP_KILL_PCT", float, 12)     # %
    EARLY_DROP_WINDOW_MIN: int = _num_env("EARLY_DROP_WINDOW_MIN", int, 7)      # min
    LIQ_CRUSH_DROP_PCT: float = _num_env("LIQ_CRUSH_DROP_PCT", float, 35)       # %
    LIQ_CRUSH_WINDOW_MIN: int = _num_env("LIQ_CRUSH_WINDOW_MIN", int, 10)       # min

    # ------- etiquetado posiciones ---------------------------------
    WIN_PCT: float = _num_env("WIN_PCT", float, 0.30)
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
    BUY_FROM_CURVE: bool = os.getenv("BUY_FROM_CURVE", "0") == "1"
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
    BANNED_CREATORS: tuple[str, ...] = tuple(x for x in os.getenv("BANNED_CREATORS", "").split(",") if x)


# instancia global inmutable
CFG = _Config()

# Aliases/herramientas de compatibilidad y export cómodos
DEXS_TTL_NIL = CFG.DEXS_TTL_NIL
DEXS_TTL_OK = CFG.DEXS_TTL_OK

# Dex/Gecko
DEX_API_BASE = CFG.DEXSCREENER_API
USE_GECKO_TERMINAL = CFG.USE_GECKO_TERMINAL
GECKO_API_URL = CFG.GECKO_API_URL

# Jupiter Price v3 (Lite) — aliases
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

# Re-queues
INCOMPLETE_RETRIES = CFG.INCOMPLETE_RETRIES
MAX_RETRIES = CFG.MAX_RETRIES

# Trading amounts
TRADE_AMOUNT_SOL = CFG.TRADE_AMOUNT_SOL
GAS_RESERVE_SOL = CFG.GAS_RESERVE_SOL
MIN_BUY_SOL = CFG.MIN_BUY_SOL
MIN_SOL_BALANCE = CFG.MIN_SOL_BALANCE

# DB / tokens
SQLITE_DB = CFG.SQLITE_DB
DB_URI = f"sqlite+aiosqlite:///{pathlib.Path(SQLITE_DB).expanduser().resolve()}"
BITQUERY_TOKEN = CFG.BITQUERY_TOKEN
PUMPFUN_PROGRAM = CFG.PUMPFUN_PROGRAM

# Riesgo/Exits (expuestos)
TAKE_PROFIT_PCT = CFG.TAKE_PROFIT_PCT
STOP_LOSS_PCT = CFG.STOP_LOSS_PCT
TRAILING_PCT = CFG.TRAILING_PCT
MAX_HOLDING_H = CFG.MAX_HOLDING_H
MAX_HARD_HOLD_H = CFG.MAX_HARD_HOLD_H
WIN_PCT = CFG.WIN_PCT           # fracción (0.30 = 30%)
LABEL_GRACE_H = CFG.LABEL_GRACE_H

# Horarios modernos
TRADING_HOURS = CFG.TRADING_HOURS
TRADING_HOURS_EXTRA = CFG.TRADING_HOURS_EXTRA
USE_EXTRA_HOURS = CFG.USE_EXTRA_HOURS
BLOCK_HOURS = CFG.BLOCK_HOURS

# Zona horaria local
LOCAL_TZ_NAME = CFG.LOCAL_TZ_NAME
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:
    # Fallback defensivo a Europe/Madrid si la zona no existe en el sistema
    LOCAL_TZ = ZoneInfo("Europe/Madrid")

# Ventanas legacy (mantener para compatibilidad en utils.time)
TRADING_WINDOWS = CFG.TRADING_WINDOWS
TRADING_WINDOWS_PARSED: tuple[tuple[int, int], ...] = _parse_windows(TRADING_WINDOWS)
TRADING_STRICT = CFG.TRADING_STRICT

# Compra / requisitos
REQUIRE_JUPITER_FOR_BUY = CFG.REQUIRE_JUPITER_FOR_BUY
DEX_WHITELIST = CFG.DEX_WHITELIST
REQUIRE_POOL_INITIALIZED = CFG.REQUIRE_POOL_INITIALIZED
BUY_RATE_LIMIT_N = CFG.BUY_RATE_LIMIT_N
BUY_RATE_LIMIT_WINDOW_S = CFG.BUY_RATE_LIMIT_WINDOW_S

# Monitor / Shadow-sim
FORCE_JUP_IN_MONITOR = CFG.FORCE_JUP_IN_MONITOR
REAL_SHADOW_SIM = CFG.REAL_SHADOW_SIM

# Señales de salida mejoradas
EARLY_DROP_KILL_PCT = CFG.EARLY_DROP_KILL_PCT
EARLY_DROP_WINDOW_MIN = CFG.EARLY_DROP_WINDOW_MIN
LIQ_CRUSH_DROP_PCT = CFG.LIQ_CRUSH_DROP_PCT
LIQ_CRUSH_WINDOW_MIN = CFG.LIQ_CRUSH_WINDOW_MIN

# IA thresholds y entreno
AI_THRESHOLD = CFG.AI_THRESHOLD             # unificado
AI_TH = AI_THRESHOLD                        # alias de compatibilidad
AI_THRESHOLD_FILE = CFG.AI_THRESHOLD_FILE
TRAIN_FORWARD_HOLDOUT_DAYS = CFG.TRAIN_FORWARD_HOLDOUT_DAYS
TRAIN_FORWARD_HOLDOUT_PCT = CFG.TRAIN_FORWARD_HOLDOUT_PCT
TRAINING_WINDOW_DAYS = CFG.TRAINING_WINDOW_DAYS
MIN_THRESHOLD_CHANGE = CFG.MIN_THRESHOLD_CHANGE

# Estrategia avanzada
REVIVAL_LIQ_USD = CFG.REVIVAL_LIQ_USD
REVIVAL_VOL1H_USD = CFG.REVIVAL_VOL1H_USD
REVIVAL_PC_5M = CFG.REVIVAL_PC_5M
BUY_FROM_CURVE = CFG.BUY_FROM_CURVE
CURVE_BUY_RANK_MAX = CFG.CURVE_BUY_RANK_MAX
CURVE_MAX_COST = CFG.CURVE_MAX_COST

# Miscelánea
BANNED_CREATORS = CFG.BANNED_CREATORS
SOL_PUBLIC_KEY = CFG.SOL_PUBLIC_KEY
LOG_LEVEL = CFG.LOG_LEVEL
LOG_PATH = CFG.LOG_PATH
FEATURES_DIR = CFG.FEATURES_DIR
MODEL_PATH = CFG.MODEL_PATH
PROJECT_ROOT = PROJECT_ROOT  # re-export
