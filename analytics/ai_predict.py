"""
analytics.ai_predict
~~~~~~~~~~~~~~~~~~~~
Inferencia en tiempo real para MemeBot 3.

• Carga «ml/model.pkl» (LightGBM/Sklearn) con joblib.
• Devuelve una probabilidad 0-1 via `should_buy(row)`.
• Soporta *hot-reload* mediante `reload_model()`.
"""

from __future__ import annotations

import pathlib
import threading
from typing import Optional

import joblib
import pandas as pd

# Ruta al modelo persistido ― relativa a la raíz del proyecto
_MODEL_PATH = pathlib.Path(__file__).resolve().parents[1] / "ml" / "model.pkl"

# ────────────────────────────── estado interno ──────────────────────────────
_model_lock = threading.Lock()
_model: Optional[object] = None          # objeto sklearn / lightgbm
_model_mtime: Optional[float] = None     # timestamp del fichero en disco


def _lazy_load() -> None:
    """
    Carga el modelo si aún no está en memoria o si el fichero ha cambiado
    desde la última vez (muy útil para *hot-reload* sin reiniciar el bot).
    """
    global _model, _model_mtime

    if not _MODEL_PATH.exists():
        # Nada entrenado todavía → fallback a probabilidad 0.0
        _model = None
        _model_mtime = None
        return

    mtime = _MODEL_PATH.stat().st_mtime
    if _model is None or mtime != _model_mtime:
        with _model_lock:           # doble-check por seguridad en entornos async
            if _model is None or mtime != _model_mtime:
                _model = joblib.load(_MODEL_PATH)
                _model_mtime = mtime
                print(f"[AI] 🧠  Modelo cargado: {_MODEL_PATH.name} (mtime={mtime})")


# Carga inicial al importar el módulo
_lazy_load()

# ─────────────────────────── API pública ────────────────────────────
def reload_model() -> None:
    """
    Fuerza la recarga desde disco (se usa tras `ml.train.train_and_save()`).
    """
    with _model_lock:
        if _MODEL_PATH.exists():
            _lazy_load()
            print("[AI] 🔄  Modelo recargado manualmente.")
        else:
            print("[AI] ⚠️  No se encontró model.pkl para recargar.")


def should_buy(row: pd.Series) -> float:
    """
    Calcula la probabilidad de compra para un único vector de características.

    Parameters
    ----------
    row : pd.Series
        Serie con las mismas columnas que se usaron al entrenar.

    Returns
    -------
    float
        Probabilidad (0-1). Si no hay modelo entrenado aún, devuelve 0.0
        para que el bot lo descarte por la capa de IA.
    """
    if not isinstance(row, pd.Series):
        raise TypeError("`row` debe ser pandas.Series")

    _lazy_load()                # comprueba si hay un modelo más nuevo en disco
    if _model is None:
        return 0.0

    df = row.to_frame().T       # → shape (1, n_features)

    # LightGBM/Sklearn compatibility: algunos modelos no exponen predict_proba
    try:
        proba = _model.predict_proba(df)[0, 1]
    except AttributeError:
        proba = _model.predict(df)[0]

    return float(proba)


__all__ = ["should_buy", "reload_model"]
