from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from config.config import PROJECT_ROOT
from ml.feature_matrix import coerce_feature_frame


def _model_path(family: str, target: str) -> Path:
    return PROJECT_ROOT / "ml" / "models" / family / f"{target}.pkl"


def _features_for_model(path: Path) -> list[str]:
    meta = path.with_suffix(".meta.json")
    if not meta.exists():
        return []
    try:
        payload = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return []
    return list(payload.get("features") or [])


def predict_model(family: str, target: str, vec: Any, *, default_features: list[str] | None = None) -> float | str | None:
    path = _model_path(family, target)
    if not path.exists():
        return None
    model = joblib.load(path)
    row = vec.to_dict() if hasattr(vec, "to_dict") else dict(vec or {})
    features = _features_for_model(path) or list(default_features or [])
    if not features:
        features = list(getattr(model, "feature_names_in_", []) or [])
    X = coerce_feature_frame(pd.DataFrame([row]), features) if features else pd.DataFrame([row])
    if hasattr(model, "predict_proba"):
        classes = list(getattr(model, "classes_", []) or [])
        if len(classes) == 2 and 1 in classes:
            return float(model.predict_proba(X)[0, classes.index(1)])
        if len(classes) == 2 and "1" in classes:
            return float(model.predict_proba(X)[0, classes.index("1")])
    pred = model.predict(X)[0]
    try:
        return float(pred)
    except Exception:
        return str(pred)


__all__ = ["predict_model"]
