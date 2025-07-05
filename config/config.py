#  config.config – MemeBot 3
from __future__ import annotations
import os, pathlib, re
from dataclasses import dataclass
from typing import Callable, TypeVar
from dotenv import load_dotenv

T = TypeVar("T", int, float)
_num_re = re.compile(r"-?\d+(?:\.\d+)?")

def _num_env(key: str, cast: Callable[[str], T], default: T) -> T:
    raw = os.getenv(key, str(default))
    m = _num_re.search(raw or "")
    try:
        return cast(m.group()) if m else default
    except (ValueError, TypeError):
        return default

# ───── carga .env ─────
PKG_DIR = pathlib.Path(__file__).resolve().parent
def _find_project_root(start: pathlib.Path) -> pathlib.Path:
    for p in [start] + list(start.parents):
        if (p / ".env").exists() or (p / "data").is_dir():
            return p
    return start

PROJECT_ROOT = _find_project_root(PKG_DIR)
load_dotenv(PROJECT_ROOT / ".env", override=True)

# ───── Config dataclass ─────
@dataclass(frozen=True)
class _Config:
    # modo
    DRY_RUN: bool = os.getenv("DRY_RUN", "0") == "1"
    TRADE_AMOUNT_SOL: float = _num_env("TRADE_AMOUNT_SOL", float, 0.1)

    # IA / ML
    AI_THRESHOLD: float = _num_env("AI_THRESHOLD", float, 0.1)
    FEATURES_DIR: pathlib.Path = pathlib.Path(os.getenv("FEATURES_DIR", PROJECT_ROOT / "data" / "features"))
    MODEL_PATH:   pathlib.Path = pathlib.Path(os.getenv("MODEL_PATH",   PROJECT_ROOT / "ml" / "model.pkl"))
    RETRAIN_DAY:  int = _num_env("RETRAIN_DAY", int, 6)
    RETRAIN_HOUR: int = _num_env("RETRAIN_HOUR", int, 4)

    # endpoints
    RPC_URL: str = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    DEXSCREENER_API: str = os.getenv("DEXSCREENER_API", "https://api.dexscreener.io")

    # Helius
    HELIUS_API_KEY: str | None = os.getenv("HELIUS_API_KEY")
    HELIUS_REST_BASE: str = os.getenv("HELIUS_REST_BASE", "https://api.helius.xyz")
    HELIUS_RPC_URL: str = os.getenv("HELIUS_RPC_URL",
        f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com")

    # otros servicios
    RUGCHECK_API_BASE: str = os.getenv("RUGCHECK_API_BASE", "https://api.rugcheck.xyz/v1")
    RUGCHECK_API_KEY: str | None = os.getenv("RUGCHECK_API_KEY")

    # ★ NUEVO: Pump Fun / Bitquery
    BITQUERY_TOKEN:    str | None = os.getenv("BITQUERY_TOKEN")          # ★
    PUMPFUN_PROGRAM:   str | None = os.getenv("PUMPFUN_PROGRAM")         # ★

    # filtros
    MAX_AGE_DAYS:      float = _num_env("MAX_AGE_DAYS", float, 2)
    MIN_HOLDERS:       int   = _num_env("MIN_HOLDERS", int, 10)
    MIN_LIQUIDITY_USD: float = _num_env("MIN_LIQUIDITY_USD", float, 5_000)
    MIN_VOL_USD_24H:   float = _num_env("MIN_VOL_USD_24H", float, 10_000)
    MAX_24H_VOLUME:    float = _num_env("MAX_24H_VOLUME", float, 1_500_000)
    MIN_SCORE_TOTAL:   int   = _num_env("MIN_SCORE_TOTAL", int, 50)

    # riesgo
    TAKE_PROFIT_PCT: float = _num_env("TAKE_PROFIT_PCT", float, 35)
    STOP_LOSS_PCT:   float = _num_env("STOP_LOSS_PCT",   float, 20)

    # estrategia avanzada
    BUY_FROM_CURVE: bool = os.getenv("BUY_FROM_CURVE", "0") == "1"
    CURVE_BUY_RANK_MAX: int = _num_env("CURVE_BUY_RANK_MAX", int, 40)
    CURVE_MAX_COST: float = _num_env("CURVE_MAX_COST", float, 1.0)
    REVIVAL_LIQ_USD: float = _num_env("REVIVAL_LIQ_USD", float, 250.0)
    REVIVAL_VOL1H_USD: float = _num_env("REVIVAL_VOL1H_USD", float, 150.0)
    REVIVAL_PC_5M: float = _num_env("REVIVAL_PC_5M", float, 15.0)

    # base de datos
    SQLITE_DB: str = os.getenv("SQLITE_DB", "data/memebotdatabase.db")

    # logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_PATH: pathlib.Path = pathlib.Path(os.getenv("LOG_PATH", PROJECT_ROOT / "logs"))

    # listas negras
    BANNED_CREATORS: tuple[str, ...] = tuple(x for x in os.getenv("BANNED_CREATORS", "").split(",") if x)

# instancia global
CFG = _Config()

# ───── aliases retro-compat ─────
DEX_API_BASE      = CFG.DEXSCREENER_API
HELIUS_API_BASE   = CFG.HELIUS_REST_BASE
HELIUS_RPC_URL    = CFG.HELIUS_RPC_URL
HELIUS_API_KEY    = CFG.HELIUS_API_KEY
RUGCHECK_API_BASE = CFG.RUGCHECK_API_BASE
RUGCHECK_API_KEY  = CFG.RUGCHECK_API_KEY
MAX_AGE_DAYS      = CFG.MAX_AGE_DAYS
MIN_HOLDERS       = CFG.MIN_HOLDERS
MIN_LIQUIDITY_USD = CFG.MIN_LIQUIDITY_USD
MIN_VOL_USD_24H   = CFG.MIN_VOL_USD_24H
MAX_24H_VOLUME    = CFG.MAX_24H_VOLUME
MIN_SCORE_TOTAL   = CFG.MIN_SCORE_TOTAL
TRADE_AMOUNT_SOL  = CFG.TRADE_AMOUNT_SOL
SQLITE_DB         = CFG.SQLITE_DB

# ★ aliases nuevos →
BITQUERY_TOKEN   = CFG.BITQUERY_TOKEN
PUMPFUN_PROGRAM  = CFG.PUMPFUN_PROGRAM
BUY_FROM_CURVE    = CFG.BUY_FROM_CURVE
CURVE_BUY_RANK_MAX = CFG.CURVE_BUY_RANK_MAX
CURVE_MAX_COST   = CFG.CURVE_MAX_COST
REVIVAL_LIQ_USD  = CFG.REVIVAL_LIQ_USD
REVIVAL_VOL1H_USD = CFG.REVIVAL_VOL1H_USD
REVIVAL_PC_5M    = CFG.REVIVAL_PC_5M
BANNED_CREATORS  = CFG.BANNED_CREATORS
