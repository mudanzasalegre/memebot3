"""
Fuente única de verdad para parámetros de salida y umbrales reexportados.

La resolución de `TAKE_PROFIT_PCT`, `WIN_PCT` y `ML_POSITIVE_PNL_PCT`
vive en `config.config`. Este módulo reexporta esos valores para mantener
imports legacy y una semántica consistente entre exits y reporting.
"""

from __future__ import annotations

from config.config import (
    LABEL_GRACE_H,
    MAX_HOLDING_H,
    ML_POSITIVE_PNL_PCT,
    ML_POSITIVE_PNL_RATIO,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TRAILING_PCT,
    WIN_PCT,
)

__all__ = [
    "WIN_PCT",
    "ML_POSITIVE_PNL_PCT",
    "ML_POSITIVE_PNL_RATIO",
    "TAKE_PROFIT_PCT",
    "STOP_LOSS_PCT",
    "TRAILING_PCT",
    "MAX_HOLDING_H",
    "LABEL_GRACE_H",
]
