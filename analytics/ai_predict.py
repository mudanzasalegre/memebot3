"""
analytics.ai_predict
~~~~~~~~~~~~~~~~~~~~
Inferencia en tiempo real para MemeBot 3.

•  Carga «ml/model.pkl» (LightGBM / sklearn) y la lista de *features*
   guardada en «ml/model.meta.json».
•  Expone:
       should_buy(vec)  →  probabilidad 0-1
       reload_model()   →  fuerza recarga en caliente
•  Convierte cualquier entrada (dict / Series / DataFrame) a un
   DataFrame de una fila con las columnas exactas que espera el modelo,
   convierte a numérico, llena NaN con 0 y hace la predicción.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

import joblib
import numpy as np
import pandas as pd

from config.config import CFG

# ───────────────────────── paths ──────────────────────────────
_MODEL_PATH: Path = CFG.MODEL_PATH
_META_PATH:  Path = _MODEL_PATH.with_suffix(".meta.json")

# ──────────────────── estado global ───────────────────────────
_model_lock = threading.Lock()
_model: Optional[Any]            = None          # objeto LightGBM / sklearn
_model_mtime: Optional[float]    = None          # timestamp del .pkl
_FEATURES: Optional[Sequence[str]] = None        # orden de columnas


# ╭────────────────── helpers internos ─────────────────╮
def _load_model() -> None:
    """Carga modelo y lista de features en memoria (lazy, thread-safe)."""
    global _model, _model_mtime, _FEATURES

    if not _MODEL_PATH.exists():        # aún no hay modelo entrenado
        _model = None
        _model_mtime = None
        _FEATURES = None
        return

    mtime = _MODEL_PATH.stat().st_mtime
    if _model is not None and mtime == _model_mtime:
        return                          # ya actualizado

    with _model_lock:
        # doble-check por concurrencia
        if _model is None or _MODEL_PATH.stat().st_mtime != _model_mtime:
            _model = joblib.load(_MODEL_PATH)
            _model_mtime = mtime

            # lista de columnas entrenadas
            if _META_PATH.exists():
                _FEATURES = json.loads(_META_PATH.read_text())["features"]
            else:                       # fallback
                _FEATURES = list(_model.feature_name())

            print(f"[AI] 🧠  Modelo cargado: {_MODEL_PATH.name} (mtime={mtime})")


def _to_dataframe(vec: Any) -> pd.DataFrame:
    """
    Convierte dict / Series / DataFrame → DataFrame de 1 fila
    con las columnas en el orden exacto de _FEATURES.
    """
    if _FEATURES is None:
        raise RuntimeError("Modelo no cargado: _FEATURES desconocido")

    if isinstance(vec, pd.DataFrame):
        X = vec[list(_FEATURES)]           # subset + orden
    else:
        if isinstance(vec, pd.Series):
            vec = vec.to_dict()
        row = {k: vec.get(k) for k in _FEATURES}
        X = pd.DataFrame([row], columns=_FEATURES)

    # cast numérico (strings → NaN) y fillna
    X = X.apply(pd.to_numeric, errors="coerce").fillna(0).astype(np.float32)
    return X


# ╭────────────────── API pública ─────────────────╮
def should_buy(vec: Any) -> float:
    """
    Devuelve la probabilidad de compra (label = 1) para el vector de características.
    •  `vec` puede ser dict, pandas.Series o pandas.DataFrame (1 fila).
    """
    _load_model()
    if _model is None:
        return 0.0

    X = _to_dataframe(vec)

    # LightGBM Booster o sklearn estimators
    try:
        proba = _model.predict_proba(X)[0, 1]   # sklearn-style
    except AttributeError:
        proba = _model.predict(X)[0]            # LightGBM Booster
    return float(proba)


def reload_model() -> None:
    """Borra el modelo en memoria para forzar recarga (p. ej. tras retrain)."""
    global _model, _model_mtime
    with _model_lock:
        _model = None
        _model_mtime = None
    _load_model()
    print("[AI] 🔄  Modelo recargado manualmente.")


__all__ = ["should_buy", "reload_model"]
