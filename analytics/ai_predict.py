# analytics/ai_predict.py
"""
Inferencia en tiempo real para MemeBot 3.

•  Carga «ml/model.pkl» (LightGBM / sklearn) y la lista de *features*
   guardada en «ml/model.meta.json».
•  Expone:
       should_buy(vec)  →  probabilidad 0-1
       reload_model()   →  fuerza recarga en caliente
•  Convierte cualquier entrada (dict / Series / DataFrame) a un
   DataFrame de una fila con las columnas exactas que espera el modelo,
   convierte a numérico, llena NaN con 0 y hace la predicción.

Nota: Este archivo ahora usa logging en vez de print para integrarse con
el sistema de logs del proyecto (utils/logger.py).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

import joblib
import numpy as np
import pandas as pd

from config.config import CFG, PROJECT_ROOT

# Logger del módulo
log = logging.getLogger("ai_predict")

# ───────────────────────── paths (robustos) ─────────────────────────
def _resolve_model_path() -> Path:
    """
    Devuelve una ruta de modelo robusta:
    - Si CFG.MODEL_PATH está vacío o es un directorio → usa PROJECT_ROOT/ml/model.pkl
    - Si no tiene sufijo .pkl → se lo añade.
    """
    p = CFG.MODEL_PATH
    # Caso vacío o ".", o nombre vacío
    if not str(p) or p.name in ("", "."):
        return (PROJECT_ROOT / "ml" / "model.pkl").resolve()

    # Si apunta a un directorio, coloca model.pkl dentro
    try:
        if p.is_dir():
            return (p / "model.pkl").resolve()
    except Exception:
        # Si la ruta no existe aún, inferimos por el sufijo
        pass

    # Si no tiene extensión, forzamos .pkl
    if not p.suffix:
        p = p.with_suffix(".pkl")

    return p.resolve()


_MODEL_PATH: Path = _resolve_model_path()


def _resolve_meta_path(mp: Path) -> Path:
    """
    Devuelve la ruta del meta:
    - Si mp tiene sufijo → mp.with_suffix(".meta.json")
    - Si no (no debería ocurrir) → <mp>.meta.json
    """
    if mp.suffix:
        return mp.with_suffix(".meta.json")
    return mp.parent / (mp.name + ".meta.json")


_META_PATH: Path = _resolve_meta_path(_MODEL_PATH)

# ──────────────────── estado global ───────────────────────────
_model_lock = threading.Lock()
_model: Optional[Any] = None               # objeto LightGBM / sklearn
_model_mtime: Optional[float] = None       # timestamp del .pkl
_FEATURES: Optional[Sequence[str]] = None  # orden de columnas


# ╭────────────────── helpers internos ─────────────────╮
def _load_model() -> None:
    """Carga modelo y lista de features en memoria (lazy, thread-safe)."""
    global _model, _model_mtime, _FEATURES

    if not _MODEL_PATH.exists():  # primera ejecución: aún no hay modelo
        _model = None
        _model_mtime = None
        _FEATURES = None
        log.debug("Modelo no encontrado en disco: %s", _MODEL_PATH)
        return

    mtime = _MODEL_PATH.stat().st_mtime
    if _model is not None and mtime == _model_mtime:
        # Ya actualizado en memoria
        return

    with _model_lock:
        # doble-check por concurrencia
        current_mtime = _MODEL_PATH.stat().st_mtime
        if _model is None or current_mtime != _model_mtime:
            _model = joblib.load(_MODEL_PATH)
            _model_mtime = current_mtime

            # lista de columnas entrenadas
            _FEATURES = None
            if _META_PATH.exists():
                try:
                    meta = json.loads(_META_PATH.read_text())
                    _FEATURES = meta.get("features")
                except Exception as e:
                    log.warning("No se pudo leer meta %s: %s", _META_PATH, e)

            # Fallback para algunos modelos (p.ej. LightGBM con atributo feature_name)
            if not _FEATURES:
                try:
                    _FEATURES = list(_model.feature_name())
                except Exception:
                    raise RuntimeError(
                        f"No se pudo determinar _FEATURES; falta {_META_PATH} "
                        "y el modelo no expone feature_name()."
                    )

            log.info("🧠 Modelo cargado: %s (mtime=%d)", _MODEL_PATH.name, int(_model_mtime))


def _to_dataframe(vec: Any) -> pd.DataFrame:
    """
    Convierte dict / Series / DataFrame → DataFrame de 1 fila
    con las columnas en el orden exacto de _FEATURES.
    """
    if _FEATURES is None:
        raise RuntimeError("Modelo no cargado o sin _FEATURES (primera ejecución).")

    if isinstance(vec, pd.DataFrame):
        X = vec[list(_FEATURES)]  # subset + orden
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
    •  Si no hay modelo aún (primera ejecución), devuelve 0.0.
    """
    _load_model()
    if _model is None:
        log.debug("Predicción omitida: no hay modelo aún, devolviendo 0.0")
        return 0.0  # primera ejecución: aún sin modelo entrenado

    X = _to_dataframe(vec)

    # LightGBM Booster o sklearn estimators
    try:
        proba = _model.predict_proba(X)[0, 1]  # sklearn-style
    except AttributeError:
        proba = _model.predict(X)[0]           # LightGBM Booster
    return float(proba)


def reload_model() -> None:
    """Borra el modelo en memoria para forzar recarga (p. ej. tras retrain)."""
    global _model, _model_mtime
    with _model_lock:
        _model = None
        _model_mtime = None
    _load_model()
    log.info("🔄 Modelo recargado manualmente (forzando reload en memoria)")


__all__ = ["should_buy", "reload_model"]
