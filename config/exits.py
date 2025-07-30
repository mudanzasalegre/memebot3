# exits.py
"""
Centraliza los parámetros de **salida** de posiciones:

    • TAKE_PROFIT_PCT   → Ganancia en porcentaje para tomar beneficios.
    • STOP_LOSS_PCT     → Pérdida en porcentaje para detener pérdidas.
    • TRAILING_PCT      → Retroceso desde el pico tras ganancia.
    • MAX_HOLDING_H     → Tiempo máximo de retención en horas.

Los valores se cargan desde .env para facilitar el ajuste sin modificar código.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Ruta al .env relativa a la raíz del proyecto
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)

def _env_float(name: str, default: float) -> float:
    """Lee una variable float desde .env con valor por defecto."""
    raw = os.getenv(name, str(default)).split()[0]
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default

def _env_int(name: str, default: int) -> int:
    """Lee una variable int desde .env con valor por defecto."""
    raw = os.getenv(name, str(default)).split()[0]
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default

# ──────────────── Parámetros globales de salida ────────────────
TAKE_PROFIT_PCT: float = _env_float("TAKE_PROFIT_PCT", 80.0)     # Ganancia ≥ X %
STOP_LOSS_PCT  : float = _env_float("STOP_LOSS_PCT",  35.0)      # Pérdida ≥ X %
TRAILING_PCT   : float = _env_float("TRAILING_PCT",   25.0)      # Retroceso desde pico
MAX_HOLDING_H  : int   = _env_int  ("MAX_HOLDING_H",   6)        # Máximo tiempo (h)

__all__ = [
    "TAKE_PROFIT_PCT",
    "STOP_LOSS_PCT",
    "TRAILING_PCT",
    "MAX_HOLDING_H",
]
