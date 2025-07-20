#  config.config – MemeBot 3
from __future__ import annotations

import os, pathlib, re
from dataclasses import dataclass
from typing import Callable, TypeVar

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
    DRY_RUN: bool  = os.getenv("DRY_RUN", "0") == "1"
    TRADE_AMOUNT_SOL: float = _num_env("TRADE_AMOUNT_SOL", float, 0.1)
    GAS_RESERVE_SOL: float  = _num_env("GAS_RESERVE_SOL",  float, 0.05)
    MIN_BUY_SOL: float      = _num_env("MIN_BUY_SOL",      float, 0.01)
    MIN_SOL_BALANCE: float  = _num_env("MIN_SOL_BALANCE",  float, 0.01)   # legacy

    # ------- IA / ML -----------------------------------------------
    AI_THRESHOLD: float = _num_env("AI_THRESHOLD", float, 0.1)
    FEATURES_DIR: pathlib.Path = pathlib.Path(
        os.getenv("FEATURES_DIR", PROJECT_ROOT / "data" / "features")
    )
    MODEL_PATH: pathlib.Path = pathlib.Path(
        os.getenv("MODEL_PATH", PROJECT_ROOT / "ml" / "model.pkl")
    )
    RETRAIN_DAY:  int = _num_env("RETRAIN_DAY",  int, 6)
    RETRAIN_HOUR: int = _num_env("RETRAIN_HOUR", int, 4)

    # ------- endpoints ---------------------------------------------
    RPC_URL:         str = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    DEXSCREENER_API: str = os.getenv("DEXSCREENER_API", "https://api.dexscreener.io")

    # ------- Helius -------------------------------------------------
    HELIUS_API_KEY:   str | None = os.getenv("HELIUS_API_KEY")
    HELIUS_REST_BASE: str = os.getenv("HELIUS_REST_BASE", "https://api.helius.xyz")
    HELIUS_RPC_URL:   str = os.getenv(
        "HELIUS_RPC_URL",
        f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com",
    )

    # ------- otros servicios ---------------------------------------
    RUGCHECK_API_BASE: str = os.getenv("RUGCHECK_API_BASE", "https://api.rugcheck.xyz/v1")
    RUGCHECK_API_KEY:  str | None = os.getenv("RUGCHECK_API_KEY")
    BITQUERY_TOKEN:    str | None = os.getenv("BITQUERY_TOKEN")
    PUMPFUN_PROGRAM:   str | None = os.getenv("PUMPFUN_PROGRAM")

    # ------- filtros ------------------------------------------------
    MAX_AGE_DAYS:       float = _num_env("MAX_AGE_DAYS",       float, 2)
    MIN_HOLDERS:        int   = _num_env("MIN_HOLDERS",        int,   10)
    MIN_LIQUIDITY_USD:  float = _num_env("MIN_LIQUIDITY_USD",  float, 5_000)
    MIN_VOL_USD_24H:    float = _num_env("MIN_VOL_USD_24H",    float, 10_000)
    MAX_24H_VOLUME:     float = _num_env("MAX_24H_VOLUME",     float, 1_500_000)
    MIN_SCORE_TOTAL:    int   = _num_env("MIN_SCORE_TOTAL",    int,   50)

    # ------- riesgo / exits ----------------------------------------
    TAKE_PROFIT_PCT: float = _num_env("TAKE_PROFIT_PCT", float, 35)
    STOP_LOSS_PCT:   float = _num_env("STOP_LOSS_PCT",   float, 20)
    TRAILING_PCT:    float = _num_env("TRAILING_PCT",    float, 30)
    MAX_HOLDING_H:   int   = _num_env("MAX_HOLDING_H",   int,   24)

    # ------- etiquetado posiciones ---------------------------------
    WIN_PCT:        float = _num_env("WIN_PCT",        float, 0.30)
    LABEL_GRACE_H:  int   = _num_env("LABEL_GRACE_H",  int,   2)

    # ------- temporizadores ----------------------------------------
    SLEEP_SECONDS:         int = _num_env("SLEEP_SECONDS",         int, 3)
    DISCOVERY_INTERVAL:    int = _num_env("DISCOVERY_INTERVAL",    int, 45)
    VALIDATION_BATCH_SIZE: int = _num_env("VALIDATION_BATCH_SIZE", int, 30)
    MAX_CANDIDATES:        int = _num_env("MAX_CANDIDATES",        int, 0)

    # ------- estrategia avanzada -----------------------------------
    BUY_FROM_CURVE:     bool  = os.getenv("BUY_FROM_CURVE", "0") == "1"
    CURVE_BUY_RANK_MAX: int   = _num_env("CURVE_BUY_RANK_MAX", int, 40)
    CURVE_MAX_COST:     float = _num_env("CURVE_MAX_COST",     float, 1.0)
    REVIVAL_LIQ_USD:    float = _num_env("REVIVAL_LIQ_USD",    float, 250.0)
    REVIVAL_VOL1H_USD:  float = _num_env("REVIVAL_VOL1H_USD",  float, 150.0)
    REVIVAL_PC_5M:      float = _num_env("REVIVAL_PC_5M",      float, 15.0)

    # ------- base de datos -----------------------------------------
    SQLITE_DB: str = os.getenv("SQLITE_DB", "data/memebotdatabase.db")

    # ------- logging -----------------------------------------------
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_PATH:  pathlib.Path = pathlib.Path(os.getenv("LOG_PATH", PROJECT_ROOT / "logs"))

    # ------- wallet / black-lists ----------------------------------
    SOL_PUBLIC_KEY: str | None = os.getenv("SOL_PUBLIC_KEY")
    BANNED_CREATORS: tuple[str, ...] = tuple(
        x for x in os.getenv("BANNED_CREATORS", "").split(",") if x
    )


# instancia global inmutable
CFG = _Config()

# ─────────────── aliases retro-compatibilidad ────────────────
DEX_API_BASE       = CFG.DEXSCREENER_API
HELIUS_API_BASE    = CFG.HELIUS_REST_BASE
HELIUS_RPC_URL     = CFG.HELIUS_RPC_URL
HELIUS_API_KEY     = CFG.HELIUS_API_KEY
RUGCHECK_API_BASE  = CFG.RUGCHECK_API_BASE
RUGCHECK_API_KEY   = CFG.RUGCHECK_API_KEY

MAX_AGE_DAYS       = CFG.MAX_AGE_DAYS
MIN_HOLDERS        = CFG.MIN_HOLDERS
MIN_LIQUIDITY_USD  = CFG.MIN_LIQUIDITY_USD
MIN_VOL_USD_24H    = CFG.MIN_VOL_USD_24H
MAX_24H_VOLUME     = CFG.MAX_24H_VOLUME
MIN_SCORE_TOTAL    = CFG.MIN_SCORE_TOTAL

TRADE_AMOUNT_SOL   = CFG.TRADE_AMOUNT_SOL
GAS_RESERVE_SOL    = CFG.GAS_RESERVE_SOL
MIN_BUY_SOL        = CFG.MIN_BUY_SOL
MIN_SOL_BALANCE    = CFG.MIN_SOL_BALANCE

SQLITE_DB          = CFG.SQLITE_DB
DB_URI             = f"sqlite+aiosqlite:///{pathlib.Path(SQLITE_DB).expanduser().resolve()}"
BITQUERY_TOKEN     = CFG.BITQUERY_TOKEN
PUMPFUN_PROGRAM    = CFG.PUMPFUN_PROGRAM

TAKE_PROFIT_PCT    = CFG.TAKE_PROFIT_PCT
STOP_LOSS_PCT      = CFG.STOP_LOSS_PCT
TRAILING_PCT       = CFG.TRAILING_PCT
MAX_HOLDING_H      = CFG.MAX_HOLDING_H
WIN_PCT            = CFG.WIN_PCT
LABEL_GRACE_H      = CFG.LABEL_GRACE_H

REVIVAL_LIQ_USD    = CFG.REVIVAL_LIQ_USD
REVIVAL_VOL1H_USD  = CFG.REVIVAL_VOL1H_USD
REVIVAL_PC_5M      = CFG.REVIVAL_PC_5M
BUY_FROM_CURVE     = CFG.BUY_FROM_CURVE
CURVE_BUY_RANK_MAX = CFG.CURVE_BUY_RANK_MAX
CURVE_MAX_COST     = CFG.CURVE_MAX_COST

BANNED_CREATORS    = CFG.BANNED_CREATORS
SOL_PUBLIC_KEY     = CFG.SOL_PUBLIC_KEY
