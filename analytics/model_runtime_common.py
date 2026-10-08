from __future__ import annotations

from hashlib import sha256
import json
import logging
from pathlib import Path
import threading
import io
from typing import Any

import joblib
import numpy as np
import pandas as pd

from config.config import PROJECT_ROOT
from ml.feature_matrix import coerce_feature_frame
from ml.financial_targets import financial_target, supported_financial_training
from analytics.inference_scope import scoped_value, scoped_snapshot, scoped_prediction
from features.context_encoding import checked_context_schema
from features.numeric_encoding import checked_numeric_schema
from features.auxiliary_semantics import checked_semantics_schema, checked_model_frame, input_frame
from features.builder import ALLOWED_FEATURES

log = logging.getLogger(__name__)
_lock = threading.RLock()
_cache: dict[tuple[str, bool], tuple[tuple[int, ...], Any, list[str], dict[str, Any]]] = {}
_registry_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def _supported_cluster_skill(evaluation: Any) -> bool:
    try:
        cluster = evaluation["cluster_skill"]
        lower = float(cluster["lower_loss_improvement"])
        mean = float(cluster["mean_loss_improvement"])
        return (cluster.get("validation_ready") is True and int(cluster["unique_tokens"]) >= 30
                and int(cluster["rows"]) >= 30 and cluster["method"] == "paired_token_cluster_bootstrap"
                and np.isfinite(lower) and np.isfinite(mean) and lower > 0 and mean > 0)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _model_path_unscoped(family: str, target: str) -> Path:
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


def _model_path(family: str, target: str) -> Path:
    return scoped_value(("family_path", str(PROJECT_ROOT), family, target),
                        lambda: _model_path_unscoped(family, target))


def _load_unscoped(path: Path, *, require_temporal_validation: bool):
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
            if not isinstance(metadata, dict):
                raise ValueError("model metadata must be an object")
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
            payload = path.read_bytes()
            if not expected_hash or sha256(payload).hexdigest() != expected_hash:
                raise ValueError("model/metadata checksum mismatch")
            features = metadata.get("features")
            if (not isinstance(features, list) or not features or len(set(features)) != len(features)
                    or any(name not in ALLOWED_FEATURES for name in features)):
                raise ValueError("model feature schema is absent")
            if (not checked_context_schema(metadata, features) or not checked_numeric_schema(metadata, features)
                    or not checked_semantics_schema(metadata, features)):
                raise ValueError("unproved specialized context encoding")
            model = joblib.load(io.BytesIO(payload))
        except Exception as exc:
            log.warning("Specialized model unavailable family=%s target=%s error=%s", path.parent.name, path.stem, type(exc).__name__)
            model, features, metadata = None, [], {}
        _cache[key] = (signature, model, features, metadata)
        return model, features, metadata


def _load(path: Path, *, require_temporal_validation: bool):
    return scoped_snapshot(("artifact", str(path), require_temporal_validation),
        lambda: _load_unscoped(path, require_temporal_validation=require_temporal_validation))


def _predict_snapshot(path: Path, model: Any, features: list[str], metadata: dict[str, Any], vec: Any,
                      *, expected_metadata: dict[str, Any] | None = None) -> float | None:
    if model is None:
        return None
    try:
        if any(metadata.get(key) != value for key, value in (expected_metadata or {}).items()):
            return None
        identity = {**metadata, **(expected_metadata or {})}
        if financial_target(identity.get("family"), identity.get("target")) and not supported_financial_training(metadata):
            return None
        X = coerce_feature_frame(checked_model_frame(input_frame(vec), features), features)
        if hasattr(model, "predict_proba"):
            if metadata.get("activation_role") == "scanner_ranking_only":
                return None
            evaluation = metadata.get("validation", {}).get("temporal", {}).get("probability_evaluation", {})
            brier = float(evaluation.get("brier_score", float("nan")))
            baseline_brier = float(evaluation.get("baseline_brier_score", float("nan")))
            if (metadata.get("prediction_kind") != "calibrated_binary_probability"
                    or metadata.get("probabilities_calibrated") is not True
                    or metadata.get("probability_validation_ready") is not True
                    or not _supported_cluster_skill(evaluation)
                    or int(evaluation.get("positive_tokens", 0)) < 5
                    or int(evaluation.get("negative_tokens", 0)) < 5
                    or not np.isfinite(brier) or not np.isfinite(baseline_brier)
                    or not 0 <= brier < baseline_brier <= 1):
                return None
            classes = list(getattr(model, "classes_", []))
            positive = 1 if 1 in classes else "1" if "1" in classes else None
            if len(classes) == 2 and positive is not None:
                value = float(scoped_prediction(("probability", str(path), id(model), metadata.get("model_sha256")),
                    X, lambda: model.predict_proba(X)[0, classes.index(positive)]))
                return value if np.isfinite(value) and 0 <= value <= 1 else None
            return None
        evaluation = metadata.get("regression_evaluation") or {}
        radius = float(evaluation.get("absolute_error_radius_pct_points", float("nan")))
        skill = float(evaluation.get("mae_skill_score", float("nan")))
        if (metadata.get("prediction_kind") != "regression_pct_points"
                or metadata.get("regression_validation_ready") is not True
                or evaluation.get("validation_ready") is not True
                or not _supported_cluster_skill(evaluation)
                or not np.isfinite(radius) or radius < 0 or not np.isfinite(skill) or skill <= 0):
            return None
        prediction = scoped_prediction(("regression", str(path), id(model), metadata.get("model_sha256")),
                                       X, lambda: model.predict(X)[0])
        value = float(prediction)
        return value if np.isfinite(value) else None
    except Exception as exc:
        log.debug("Specialized prediction unavailable family=%s target=%s error=%s", path.parent.name, path.stem, type(exc).__name__)
        return None


def predict_artifact(path: Path, vec: Any, *, require_temporal_validation: bool = True,
                     expected_metadata: dict[str, Any] | None = None) -> float | None:
    """One checked reader; compatibility artifacts cannot bypass OOS skill."""
    model, features, metadata = _load(path, require_temporal_validation=require_temporal_validation)
    return _predict_snapshot(path, model, features, metadata, vec, expected_metadata=expected_metadata)


def predict_model(family: str, target: str, vec: Any, *, default_features: list[str] | None = None,
                  require_temporal_validation: bool = True) -> float | str | None:
    """Advisory prediction; absence/corruption is unknown, not fabricated zero."""
    return predict_artifact(_model_path(family, target), vec,
                            require_temporal_validation=require_temporal_validation,
                            expected_metadata={"family": family, "target": target})


def predict_regression_estimate(family: str, target: str, vec: Any) -> dict[str, Any]:
    # Read one verified snapshot. A concurrent retrain must not combine an old
    # point prediction with a new model's error metadata.
    path = _model_path(family, target)
    model, features, metadata = _load(path, require_temporal_validation=True)
    value = _predict_snapshot(path, model, features, metadata, vec,
                              expected_metadata={"family": family, "target": target,
                                                 "prediction_kind": "regression_pct_points"})
    result = {"value": value, "lower": None, "upper": None, "error_radius_pct_points": None,
              "status": "unknown" if value is None else "validated_advisory_estimate",
              "unit": "percentage_points", "future_coverage_guaranteed": False}
    if financial_target(family, target):
        result["return_basis"] = (metadata.get("financial_training") or {}).get("return_basis") if value is not None else None
        result["financial_scope"] = "estimated_paper_execution_not_live_profit" if value is not None else "unknown"
    if value is None:
        return result
    try:
        evaluation = metadata["regression_evaluation"]
        radius = float(evaluation["absolute_error_radius_pct_points"])
        if not np.isfinite(radius) or radius < 0:
            raise ValueError("invalid empirical error radius")
        lower, upper = float(value) - radius, float(value) + radius
        if not np.isfinite(lower) or not np.isfinite(upper):
            raise ValueError("nonfinite empirical error bounds")
        result.update(lower=lower, upper=upper,
                      error_radius_pct_points=radius, interval_method=evaluation["interval_method"])
    except (TypeError, ValueError, KeyError, OverflowError):
        result.update(value=None, status="unknown")
    return result


def invalidate_model_cache(path: Path) -> None:
    with _lock:
        for key in list(_cache):
            if key[0] == str(path):
                _cache.pop(key, None)


def predict_ranking_score(family: str, target: str, vec: Any) -> float | None:
    """Validated rank percentile (0-100), explicitly not an event probability."""
    model, features, metadata = _load(_model_path(family, target), require_temporal_validation=True)
    if model is None or not metadata.get("ranking_validation_ready"):
        return None
    if financial_target(family, target) and not supported_financial_training(metadata):
        return None
    try:
        reference = np.asarray(metadata.get("rank_reference_quantiles") or [], dtype=float)
        if len(reference) < 2 or not np.isfinite(reference).all() or np.any(np.diff(reference) < 0):
            return None
        X = coerce_feature_frame(checked_model_frame(input_frame(vec), features), features)
        value = float(scoped_prediction(("ranking", id(model), metadata.get("model_sha256")),
                                       X, lambda: model.rank_score(X)[0]))
        if not np.isfinite(value):
            return None
        low = np.searchsorted(reference, value, side="left")
        high = np.searchsorted(reference, value, side="right")
        return min(100.0, max(0.0, (low + high) / 2 / len(reference) * 100))
    except Exception:
        return None


__all__ = ["predict_model", "predict_artifact", "predict_regression_estimate", "predict_ranking_score", "invalidate_model_cache"]
