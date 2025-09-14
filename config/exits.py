# config/exits.py
"""
Centraliza los parámetros de **salida** de posiciones y los alinea con el etiquetado.

Parámetros expuestos (todos desde .env, con defaults sensatos):
    • WIN_PCT           → Umbral de ganancia **fraccional** usado por el labeler (p.ej. 0.30 = 30%).
    • TAKE_PROFIT_PCT   → Ganancia en **porcentaje** para tomar beneficios (p.ej. 30.0 = 30%).
    • STOP_LOSS_PCT     → Pérdida en porcentaje para detener pérdidas.
    • TRAILING_PCT      → Retroceso desde el pico tras ganancia.
    • MAX_HOLDING_H     → Tiempo máximo de retención en horas.
    • LABEL_GRACE_H     → Ventana de gracia tras el cierre antes de etiquetar.

Alineación WIN_PCT ↔ TAKE_PROFIT_PCT:
    - Si ambos están en .env y difieren, se **prioriza TAKE_PROFIT_PCT** y se ajusta WIN_PCT=TAKE_PROFIT_PCT/100.
    - Si solo está WIN_PCT, derivamos TAKE_PROFIT_PCT = WIN_PCT*100.
    - Si solo está TAKE_PROFIT_PCT, derivamos WIN_PCT = TAKE_PROFIT_PCT/100.

Esto garantiza que la estrategia de salidas y el etiquetado usen el **mismo umbral**.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Ruta al .env relativa a la raíz del proyecto
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)


# ───────────── helpers env ─────────────
def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw.split()[0])
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw.split()[0])
    except (ValueError, TypeError):
        return default


def _env_raw(name: str) -> Optional[str]:
    raw = os.getenv(name)
    return raw.strip() if isinstance(raw, str) else None


# ───────────── lectura base ─────────────
# 1) Leer posibles fuentes
_raw_win = _env_raw("WIN_PCT")               # fracción (0.30)
_raw_tp  = _env_raw("TAKE_PROFIT_PCT")       # porcentaje (30.0)

# 2) Normalizar con reglas de alineación
if _raw_tp is not None and _raw_tp != "":
    # Si hay TP explícito, se prioriza y derivamos WIN_PCT
    try:
        _tp_val = float(_raw_tp.split()[0])
    except Exception:
        _tp_val = 30.0
    TAKE_PROFIT_PCT: float = _tp_val
    WIN_PCT: float = _tp_val / 100.0
else:
    # No hay TP explícito; tomar WIN_PCT (o default) y derivar TP
    win_val = _env_float("WIN_PCT", 0.30)
    WIN_PCT = win_val
    TAKE_PROFIT_PCT = round(win_val * 100.0, 6)

# 3) Resto de parámetros (con defaults)
STOP_LOSS_PCT  : float = _env_float("STOP_LOSS_PCT", 35.0)  # Pérdida ≥ X %
TRAILING_PCT   : float = _env_float("TRAILING_PCT", 25.0)   # Retroceso desde pico
MAX_HOLDING_H  : int   = _env_int  ("MAX_HOLDING_H", 6)     # Máximo tiempo (h)
LABEL_GRACE_H  : int   = _env_int  ("LABEL_GRACE_H", 2)     # Gracia para etiquetar (h)

# Export público
__all__ = [
    "WIN_PCT",           # fracción (0.30 = 30%)
    "TAKE_PROFIT_PCT",   # porcentaje (30.0 = 30%)
    "STOP_LOSS_PCT",
    "TRAILING_PCT",
    "MAX_HOLDING_H",
    "LABEL_GRACE_H",
]
