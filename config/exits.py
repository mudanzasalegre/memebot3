"""
Centraliza los parámetros de **salida** de posiciones:

    • TAKE_PROFIT_PCT
    • STOP_LOSS_PCT
    • TRAILING_PCT
    • MAX_HOLDING_H

Se cargan de .env para permitir tuning sin tocar código.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Cargamos .env si este módulo es importado antes que config.config
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).split()[0]
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).split()[0]
    try:
        return int(raw)
    except ValueError:
        return default


# ────────────────────── parámetros de salida ───────────────────
TAKE_PROFIT_PCT: float = _env_float("TAKE_PROFIT_PCT", 80.0)
STOP_LOSS_PCT  : float = _env_float("STOP_LOSS_PCT",  35.0)
TRAILING_PCT   : float = _env_float("TRAILING_PCT",   25.0)
MAX_HOLDING_H  : int   = _env_int  ("MAX_HOLDING_H",   6)

__all__ = [
    "TAKE_PROFIT_PCT",
    "STOP_LOSS_PCT",
    "TRAILING_PCT",
    "MAX_HOLDING_H",
]
