# config/config.py – MemeBot 3
"""
Añadidos 2025-08-02
──────────────────
• GECKO_API_URL          (endpoint base GeckoTerminal)
• INCOMPLETE_RETRIES     (reintentos rápidos 0-delay en run_bot)
• MAX_RETRIES            (re-queues permitidos en utils.lista_pares)

Añadidos 2025-08-15
──────────────────
• USE_JUPITER_PRICE      (activar fallback Jupiter Price v3 Lite)
• JUPITER_PRICE_URL      (endpoint base)
• JUPITER_RPM            (rate-limit sencillo en fetcher/jupiter_price)
• JUPITER_TTL_NIL_SHORT  (TTL caché negativa corto)
• JUPITER_TTL_NIL_MAX    (TTL caché negativa máximo)
• JUPITER_TTL_OK         (TTL caché positiva, opcional)

Añadidos 2025-08-23
──────────────────
• TRADING_WINDOWS        (ventanas horarias de compra, ej. "13-16" o "7,11,13-16,18,22")
• TRADING_STRICT         (si true, fuera de ventana no compra; si false, solo “degrada”)
• REQUIRE_JUPITER_FOR_BUY (exigir Jupiter para “solo precio” en la entrada)
• EARLY_DROP_KILL_PCT/EARLY_DROP_WINDOW_MIN (salida temprana por caída)
• LIQ_CRUSH_DROP_PCT/LIQ_CRUSH_WINDOW_MIN   (salida por derrumbe de liquidez)
• AI_THRESHOLD_FILE       (ruta al umbral recomendado post-entrenamiento)
"""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass
from typing import Callable, TypeVar, Tuple

from dotenv import load_dotenv

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


def _parse_windows(raw: str) -> tuple[tuple[int, int], ...]:
    """
    Parseo robusto de TRADING_WINDOWS.
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
    # Merge intervalos solapados y ordenar
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
    MIN_SOL_BALANCE: float = _num_env("MIN_SOL_BALANCE", float, 0.01)  # legacy
    DEXS_TTL_NIL: int = _num_env("DEXS_TTL_NIL", int, 300)

    # ------- IA / ML -----------------------------------------------
    AI_THRESHOLD: float = _num_env("AI_THRESHOLD", float, 0.1)
    FEATURES_DIR: pathlib.Path = pathlib.Path(
        os.getenv("FEATURES_DIR", PROJECT_ROOT / "data" / "features")
    )
    MODEL_PATH: pathlib.Path = pathlib.Path(
        os.getenv("MODEL_PATH", PROJECT_ROOT / "ml" / "model.pkl")
    )
    # Archivo con umbral IA recomendado (si existe, puede sobrescribir AI_THRESHOLD al arrancar)
    AI_THRESHOLD_FILE: pathlib.Path = pathlib.Path(
        os.getenv("AI_THRESHOLD_FILE", PROJECT_ROOT / "data" / "metrics" / "recommended_threshold.json")
    )
    RETRAIN_DAY: int = _num_env("RETRAIN_DAY", int, 6)
    RETRAIN_HOUR: int = _num_env("RETRAIN_HOUR", int, 4)

    # ------- endpoints ---------------------------------------------
    RPC_URL: str = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    # Compat: permite DEXSCREENER_API o DEX_API_BASE (env antiguo)
    DEXSCREENER_API: str = (
        os.getenv("DEXSCREENER_API")
        or os.getenv("DEX_API_BASE")
        or "https://api.dexscreener.com"
    )

    # ------- Helius -------------------------------------------------
    HELIUS_API_KEY: str | None = os.getenv("HELIUS_API_KEY")
    # Compat: HELIUS_REST_BASE o HELIUS_API_BASE (env antiguo)
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
    RUGCHECK_API_BASE: str = os.getenv(
        "RUGCHECK_API_BASE", "https://api.rugcheck.xyz/v1"
    )
    RUGCHECK_API_KEY: str | None = os.getenv("RUGCHECK_API_KEY")
    BITQUERY_TOKEN: str | None = os.getenv("BITQUERY_TOKEN")
    PUMPFUN_PROGRAM: str | None = os.getenv("PUMPFUN_PROGRAM")

    # ------- GeckoTerminal -----------------------------------------
    USE_GECKO_TERMINAL: bool = os.getenv("USE_GECKO_TERMINAL", "true").lower() == "true"
    GECKO_API_URL: str = os.getenv(
        "GECKO_API_URL", "https://api.geckoterminal.com/api/v2"
    )
    GECKO_SOL_ENDPOINT: str = f"{GECKO_API_URL}/networks/solana/pools"

    # ------- Jupiter Price v3 (Lite) -------------------------------
    USE_JUPITER_PRICE: bool = os.getenv("USE_JUPITER_PRICE", "true").lower() == "true"
    JUPITER_PRICE_URL: str = os.getenv(
        "JUPITER_PRICE_URL",
        "https://lite-api.jup.ag/price/v3",
    )
    JUPITER_RPM: int = _num_env("JUPITER_RPM", int, 60)           # 60 req/min (Lite)
    JUPITER_TTL_NIL_SHORT: int = _num_env("JUPITER_TTL_NIL_SHORT", int, 120)
    JUPITER_TTL_NIL_MAX: int = _num_env("JUPITER_TTL_NIL_MAX", int, 600)
    # Opcional (caché OK). Si no quieres exponerlo, bórralo: fetcher tiene default 120.
    JUPITER_TTL_OK: int = _num_env("JUPITER_TTL_OK", int, 120)

    # ------- filtros ------------------------------------------------
    MAX_AGE_DAYS: float = _num_env("MAX_AGE_DAYS", float, 2)
    MIN_AGE_MIN: float = _num_env("MIN_AGE_MIN", float, 2.0)
    MIN_HOLDERS: int = _num_env("MIN_HOLDERS", int, 10)
    MIN_LIQUIDITY_USD: float = _num_env("MIN_LIQUIDITY_USD", float, 5_000)
    MIN_VOL_USD_24H: float = _num_env("MIN_VOL_USD_24H", float, 10_000)

    MIN_MARKET_CAP_USD: float = _num_env("MIN_MARKET_CAP_USD", float, 5_000)
    MAX_MARKET_CAP_USD: float = _num_env("MAX_MARKET_CAP_USD", float, 20_000)

    MAX_24H_VOLUME: float = _num_env("MAX_24H_VOLUME", float, 1_500_000)
    MIN_SCORE_TOTAL: int = _num_env("MIN_SCORE_TOTAL", int, 50)
    MAX_ACTIVE_POSITIONS: int = _num_env("MAX_ACTIVE_POSITIONS", int, 25)

    # ------- trading windows / compra -------------------------------
    # Por defecto activa 13:00–16:59 (hora local del sistema)
    TRADING_WINDOWS: str = os.getenv("TRADING_WINDOWS", "13-16")
    TRADING_STRICT: bool = os.getenv("TRADING_STRICT", "true").lower() == "true"
    # Requisito de fuente de precio en entrada (solo precio)
    REQUIRE_JUPITER_FOR_BUY: bool = os.getenv("REQUIRE_JUPITER_FOR_BUY", "true").lower() == "true"

    # ------- riesgo / exits ----------------------------------------
    TAKE_PROFIT_PCT: float = _num_env("TAKE_PROFIT_PCT", float, 35)
    STOP_LOSS_PCT: float = _num_env("STOP_LOSS_PCT", float, 20)
    TRAILING_PCT: float = _num_env("TRAILING_PCT", float, 30)
    MAX_HOLDING_H: int = _num_env("MAX_HOLDING_H", int, 24)

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
    LOG_PATH: pathlib.Path = pathlib.Path(
        os.getenv("LOG_PATH", PROJECT_ROOT / "logs")
    )

    # ------- wallet / black-lists ----------------------------------
    SOL_PUBLIC_KEY: str | None = os.getenv("SOL_PUBLIC_KEY")
    BANNED_CREATORS: tuple[str, ...] = tuple(
        x for x in os.getenv("BANNED_CREATORS", "").split(",") if x
    )


# instancia global inmutable
CFG = _Config()

DEXS_TTL_NIL = CFG.DEXS_TTL_NIL

# ─────────────── aliases retro-compatibilidad ────────────────
DEX_API_BASE = CFG.DEXSCREENER_API
USE_GECKO_TERMINAL = CFG.USE_GECKO_TERMINAL
GECKO_API_URL = CFG.GECKO_API_URL

# Jupiter Price v3 (Lite) — aliases
USE_JUPITER_PRICE = CFG.USE_JUPITER_PRICE
JUPITER_PRICE_URL = CFG.JUPITER_PRICE_URL
JUPITER_RPM = CFG.JUPITER_RPM
JUPITER_TTL_NIL_SHORT = CFG.JUPITER_TTL_NIL_SHORT
JUPITER_TTL_NIL_MAX = CFG.JUPITER_TTL_NIL_MAX
JUPITER_TTL_OK = CFG.JUPITER_TTL_OK  # opcional

HELIUS_API_BASE = CFG.HELIUS_REST_BASE
HELIUS_RPC_URL = CFG.HELIUS_RPC_URL
HELIUS_API_KEY = CFG.HELIUS_API_KEY
RUGCHECK_API_BASE = CFG.RUGCHECK_API_BASE
RUGCHECK_API_KEY = CFG.RUGCHECK_API_KEY

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

# re-queues
INCOMPLETE_RETRIES = CFG.INCOMPLETE_RETRIES
MAX_RETRIES = CFG.MAX_RETRIES

TRADE_AMOUNT_SOL = CFG.TRADE_AMOUNT_SOL
GAS_RESERVE_SOL = CFG.GAS_RESERVE_SOL
MIN_BUY_SOL = CFG.MIN_BUY_SOL
MIN_SOL_BALANCE = CFG.MIN_SOL_BALANCE

SQLITE_DB = CFG.SQLITE_DB
DB_URI = f"sqlite+aiosqlite:///{pathlib.Path(SQLITE_DB).expanduser().resolve()}"
BITQUERY_TOKEN = CFG.BITQUERY_TOKEN
PUMPFUN_PROGRAM = CFG.PUMPFUN_PROGRAM

TAKE_PROFIT_PCT = CFG.TAKE_PROFIT_PCT
STOP_LOSS_PCT = CFG.STOP_LOSS_PCT
TRAILING_PCT = CFG.TRAILING_PCT
MAX_HOLDING_H = CFG.MAX_HOLDING_H
WIN_PCT = CFG.WIN_PCT
LABEL_GRACE_H = CFG.LABEL_GRACE_H

# Ventanas/compra y riesgo extendido
TRADING_WINDOWS = CFG.TRADING_WINDOWS
TRADING_WINDOWS_PARSED: tuple[tuple[int, int], ...] = _parse_windows(TRADING_WINDOWS)
TRADING_STRICT = CFG.TRADING_STRICT
REQUIRE_JUPITER_FOR_BUY = CFG.REQUIRE_JUPITER_FOR_BUY

EARLY_DROP_KILL_PCT = CFG.EARLY_DROP_KILL_PCT
EARLY_DROP_WINDOW_MIN = CFG.EARLY_DROP_WINDOW_MIN
LIQ_CRUSH_DROP_PCT = CFG.LIQ_CRUSH_DROP_PCT
LIQ_CRUSH_WINDOW_MIN = CFG.LIQ_CRUSH_WINDOW_MIN

# IA threshold file (para que run_bot pueda leerlo si existe)
AI_THRESHOLD = CFG.AI_THRESHOLD
AI_THRESHOLD_FILE = CFG.AI_THRESHOLD_FILE

REVIVAL_LIQ_USD = CFG.REVIVAL_LIQ_USD
REVIVAL_VOL1H_USD = CFG.REVIVAL_VOL1H_USD
REVIVAL_PC_5M = CFG.REVIVAL_PC_5M
BUY_FROM_CURVE = CFG.BUY_FROM_CURVE
CURVE_BUY_RANK_MAX = CFG.CURVE_BUY_RANK_MAX
CURVE_MAX_COST = CFG.CURVE_MAX_COST

BANNED_CREATORS = CFG.BANNED_CREATORS
SOL_PUBLIC_KEY = CFG.SOL_PUBLIC_KEY
LOG_LEVEL = CFG.LOG_LEVEL
LOG_PATH = CFG.LOG_PATH
FEATURES_DIR = CFG.FEATURES_DIR
MODEL_PATH = CFG.MODEL_PATH
PROJECT_ROOT = PROJECT_ROOT  # re-export por comodidad
