from __future__ import annotations

from hashlib import sha256
import json
import logging
from pathlib import Path
import threading
from typing import Any

import joblib
import numpy as np
import pandas as pd

from config.config import PROJECT_ROOT
from ml.feature_matrix import coerce_feature_frame

log = logging.getLogger(__name__)
_lock = threading.RLock()
_cache: dict[tuple[str, bool], tuple[tuple[int, ...], Any, list[str], dict[str, Any]]] = {}
_registry_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def _model_path(family: str, target: str) -> Path:
    directory = PROJECT_ROOT / "ml" / "models" / family
    manifest_path = directory / "advisory_manifest.json"
    if not manifest_path.exists():
        return directory / f"{target}.pkl"
    unavailable = directory / "_unavailable_" / f"{target}.pkl"
    try:
        stat = manifest_path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        with _lock:
            cached = _registry_cache.get(str(manifest_path))
            if cached is None or cached[0] != signature:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                _registry_cache[str(manifest_path)] = (signature, manifest)
            else:
                manifest = cached[1]
        if manifest.get("role") != "scanner_ranking_only":
            return unavailable
        relative = manifest.get("heads", {}).get(target, {}).get("path")
        if not relative:
            return unavailable
        path = (directory / relative).resolve()
        if not path.is_relative_to((directory / "versions").resolve()) or path.name != f"{target}.pkl":
            return unavailable
        return path
    except Exception:
        return unavailable


def _load(path: Path, *, require_temporal_validation: bool):
    meta_path = path.with_suffix(".meta.json")
    key = (str(path), require_temporal_validation)
    try:
        model_stat, meta_stat = path.stat(), meta_path.stat()
    except OSError:
        with _lock:
            _cache.pop(key, None)
        return None, [], {}
    signature = (model_stat.st_mtime_ns, model_stat.st_size, meta_stat.st_mtime_ns, meta_stat.st_size)
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1], cached[2], cached[3]
        model, features, metadata = None, [], {}
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            validation = metadata.get("validation") or {}
            temporal = validation.get("temporal") or {}
            if path.parent.parent.name == "versions" and path.parent.parent.parent.name == "runner" and metadata.get("activation_role") != "scanner_ranking_only":
                raise ValueError("versioned runner artifact lacks its ranking-only role")
            if require_temporal_validation and (
                validation.get("mode") != "purged_token_walk_forward"
                or int(temporal.get("out_of_sample_rows") or 0) < 10
            ):
                _cache[key] = (signature, None, [], metadata)
                return None, [], metadata
            expected_hash = metadata.get("model_sha256")
            if not expected_hash or sha256(path.read_bytes()).hexdigest() != expected_hash:
                raise ValueError("model/metadata checksum mismatch")
            features = list(dict.fromkeys(metadata.get("features") or []))
            if not features:
                raise ValueError("model feature schema is absent")
            model = joblib.load(path)
        except Exception as exc:
            log.warning("Specialized model unavailable family=%s target=%s error=%s", path.parent.name, path.stem, type(exc).__name__)
            model, features = None, []
        _cache[key] = (signature, model, features, metadata)
        return model, features, metadata


def predict_model(family: str, target: str, vec: Any, *, default_features: list[str] | None = None,
                  require_temporal_validation: bool = True) -> float | str | None:
    """Advisory prediction; absence/corruption is unknown, not fabricated zero."""
    model, features, metadata = _load(_model_path(family, target), require_temporal_validation=require_temporal_validation)
    if model is None:
        return None
    try:
        row = vec.to_dict() if hasattr(vec, "to_dict") else dict(vec or {})
        X = coerce_feature_frame(pd.DataFrame([row]), features)
        if hasattr(model, "predict_proba"):
            if metadata.get("activation_role") == "scanner_ranking_only":
                return None
            if not metadata.get("probabilities_calibrated") or not metadata.get("probability_validation_ready"):
                return None
            classes = list(getattr(model, "classes_", []))
            positive = 1 if 1 in classes else "1" if "1" in classes else None
            if len(classes) == 2 and positive is not None:
                value = float(model.predict_proba(X)[0, classes.index(positive)])
                return value if np.isfinite(value) and 0 <= value <= 1 else None
        prediction = model.predict(X)[0]
        if isinstance(prediction, str):
            return prediction
        value = float(prediction)
        return value if np.isfinite(value) else None
    except Exception as exc:
        log.debug("Specialized prediction unavailable family=%s target=%s error=%s", family, target, type(exc).__name__)
        return None


def predict_ranking_score(family: str, target: str, vec: Any) -> float | None:
    """Validated rank percentile (0-100), explicitly not an event probability."""
    model, features, metadata = _load(_model_path(family, target), require_temporal_validation=True)
    if model is None or not metadata.get("ranking_validation_ready"):
        return None
    try:
        reference = np.asarray(metadata.get("rank_reference_quantiles") or [], dtype=float)
        if len(reference) < 2 or not np.isfinite(reference).all() or np.any(np.diff(reference) < 0):
            return None
        row = vec.to_dict() if hasattr(vec, "to_dict") else dict(vec or {})
        X = coerce_feature_frame(pd.DataFrame([row]), features)
        value = float(model.rank_score(X)[0])
        if not np.isfinite(value):
            return None
        low = np.searchsorted(reference, value, side="left")
        high = np.searchsorted(reference, value, side="right")
        return min(100.0, max(0.0, (low + high) / 2 / len(reference) * 100))
    except Exception:
        return None


__all__ = ["predict_model", "predict_ranking_score"]
