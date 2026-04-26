from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from config.config import PROJECT_ROOT
from ml.feature_matrix import coerce_feature_frame

log = logging.getLogger("ev_predict")

MODEL_PATH = PROJECT_ROOT / "ml" / "ev_model.pkl"
META_PATH = PROJECT_ROOT / "ml" / "ev_model.meta.json"
_lock = threading.Lock()
_model: Any | None = None
_mtime: float | None = None
_features: list[str] | None = None


def _load() -> None:
    global _model, _mtime, _features
    if not MODEL_PATH.exists():
        _model = None
        _mtime = None
        _features = None
        return
    mtime = MODEL_PATH.stat().st_mtime
    if _model is not None and _mtime == mtime:
        return
    with _lock:
        _model = joblib.load(MODEL_PATH)
        _mtime = mtime
        meta = {}
        if META_PATH.exists():
            try:
                meta = json.loads(META_PATH.read_text(encoding="utf-8")) or {}
            except Exception:
                meta = {}
        _features = list(meta.get("features") or getattr(_model, "feature_name_", []) or [])


def predict_ev(vec: Any) -> float | None:
    _load()
    if _model is None or not _features:
        return None
    row = vec.to_dict() if hasattr(vec, "to_dict") else dict(vec)
    X = coerce_feature_frame(pd.DataFrame([row]), _features)
    return float(_model.predict(X)[0])


def reload_ev_model() -> None:
    global _model, _mtime, _features
    with _lock:
        _model = None
        _mtime = None
        _features = None
    _load()


__all__ = ["predict_ev", "reload_ev_model"]
