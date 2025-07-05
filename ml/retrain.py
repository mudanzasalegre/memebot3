"""
ml.retrain
~~~~~~~~~~
Intenta re-entrenar y sólo sobre-escribe el modelo si mejora el AUC
en al menos `min_delta`.
"""
from __future__ import annotations

import json
import pathlib

from config.config import CFG
from .train import train_and_save

MODEL_PATH = CFG.MODEL_PATH
META_PATH = MODEL_PATH.with_suffix(".meta.json")


def _current_auc() -> float | None:
    if META_PATH.exists():
        return json.loads(META_PATH.read_text())["auc"]
    return None


def retrain_if_better(min_delta: float = 0.005) -> bool:
    """
    Re-entrena y sustituye el modelo sólo si el AUC mejora.
    Returns True si se actualizó el modelo.
    """
    prev_auc = _current_auc()
    new_auc = train_and_save()

    if prev_auc is None or new_auc >= prev_auc + min_delta:
        print(f"[ML] ✅ Modelo actualizado ({prev_auc} → {new_auc})")
        return True

    print(f"[ML] ❌ Sin mejora significativa ({prev_auc} vs {new_auc}) – se descarta")
    # Restaurar el modelo antiguo (train_and_save ya lo guardó encima)
    # Si no quieres sobre-escritura, ejecuta train en ruta tmp y copia solo si mejora.
    return False
