from __future__ import annotations

from hashlib import sha256
from copy import deepcopy
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
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
from analytics.decision_provenance import record_model_query
from features.context_encoding import checked_context_schema
from features.numeric_encoding import checked_numeric_schema
from features.auxiliary_semantics import checked_semantics_schema, checked_model_frame, input_frame
from features.builder import ALLOWED_FEATURES
from ml.exit_diagnostics import checked_exit_metadata
from ml.model_validation_warnings import RANKING_METRIC_VERSION, ranking_token_skill_ready

log = logging.getLogger(__name__)
_lock = threading.RLock()
_cache: dict[tuple[Any, ...], tuple[tuple[Any, ...], Any, list[str], dict[str, Any]]] = {}
_SAFE_NAME = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_FAMILY_HEADS = 64


@dataclass(frozen=True)
class _HeadReference:
    target: str
    path: Path
    model_sha256: str | None = None
    metadata_sha256: str | None = None
    version: str | None = None
    valid: bool = True


@dataclass(frozen=True)
class _FamilySelection:
    family: str
    mode: str
    references: tuple[_HeadReference, ...] = ()
    manifest_sha256: str | None = None


def _family_selection_unscoped(family: str) -> _FamilySelection:
    """Read the whole selector once, including its absence or invalidity."""
    if not isinstance(family, str) or not _SAFE_NAME.fullmatch(family):
        return _FamilySelection(str(family), "unavailable")
    directory = PROJECT_ROOT / "ml" / "models" / family
    manifest_path = directory / "advisory_manifest.json"
    try:
        if not manifest_path.exists():
            paths = sorted(directory.glob("*.pkl"))
            if len(paths) > MAX_FAMILY_HEADS:
                return _FamilySelection(family, "unavailable")
            return _FamilySelection(family, "legacy_flat", tuple(
                _HeadReference(path.stem, path) for path in paths if _SAFE_NAME.fullmatch(path.stem)))
        payload = manifest_path.read_bytes()
        manifest = json.loads(payload)
        digest = sha256(payload).hexdigest()
        if not isinstance(manifest, dict) or manifest.get("role") != "scanner_ranking_only":
            return _FamilySelection(family, "unavailable", manifest_sha256=digest)
        heads = manifest.get("heads")
        if not isinstance(heads, dict) or len(heads) > MAX_FAMILY_HEADS:
            return _FamilySelection(family, "unavailable", manifest_sha256=digest)
        references = []
        for target, entry in sorted(heads.items()):
            if not isinstance(target, str) or not _SAFE_NAME.fullmatch(target) or not isinstance(entry, dict):
                continue
            relative = entry.get("path")
            if not isinstance(relative, str):
                continue
            try:
                path = (directory / relative).resolve()
                parts = path.relative_to(directory.resolve()).parts
            except (OSError, ValueError):
                continue
            if (len(parts) != 3 or parts[0] != "versions" or path.name != f"{target}.pkl"
                    or not path.is_relative_to((directory / "versions").resolve())):
                continue
            checksum, metadata_checksum, version = entry.get("model_sha256"), entry.get("metadata_sha256"), entry.get("version")
            valid = (isinstance(checksum, str) and _SHA256.fullmatch(checksum) is not None
                     and isinstance(metadata_checksum, str) and _SHA256.fullmatch(metadata_checksum) is not None
                     and isinstance(version, str) and version == parts[1])
            references.append(_HeadReference(target, path, model_sha256=checksum,
                metadata_sha256=metadata_checksum, version=version, valid=valid))
        return _FamilySelection(family, "manifest", tuple(references), digest)
    except (OSError, ValueError, TypeError):
        return _FamilySelection(family, "unavailable")


def _family_selection(family: str) -> _FamilySelection:
    return scoped_value(("family_selection", str(PROJECT_ROOT), family),
                        lambda: _family_selection_unscoped(family))


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
    selection = _family_selection_unscoped(family)
    return _selected_path(selection, target)


def _selected_path(selection: _FamilySelection, target: str) -> Path:
    for reference in selection.references:
        if reference.target == target:
            return reference.path
    directory = PROJECT_ROOT / "ml" / "models" / selection.family
    if selection.mode == "legacy_flat":
        return directory / f"{target}.pkl"
    return directory / "_unavailable_" / f"{target}.pkl"


def _model_path(family: str, target: str) -> Path:
    return _selected_path(_family_selection(family), target)


def _artifact_signature(path: Path):
    try:
        model, meta = path.stat(), path.with_suffix(".meta.json").stat()
        return (model.st_mtime_ns, model.st_size, meta.st_mtime_ns, meta.st_size,
            sha256(path.read_bytes()).hexdigest(), sha256(path.with_suffix(".meta.json").read_bytes()).hexdigest())
    except OSError:
        return None


def _capture_family(selection: _FamilySelection, *, require_temporal_validation: bool):
    """Pin all selected heads before the first prediction, not one at a time.

    Flat compatibility artifacts have no published training-generation claim.
    Require a stable file collection while capturing their checked snapshots.
    Versioned heads may intentionally come from different accepted training
    cohorts; the exact common manifest, not a fabricated common fit, is bound.
    """
    before = {reference.target: _artifact_signature(reference.path) for reference in selection.references}
    snapshots = {}
    for reference in selection.references:
        model, features, metadata = (None, [], {})
        if reference.valid:
            model, features, metadata = _load_unscoped(reference.path,
                require_temporal_validation=require_temporal_validation,
                expected_model_sha256=reference.model_sha256 if selection.mode == "manifest" else None,
                expected_metadata_sha256=reference.metadata_sha256 if selection.mode == "manifest" else None,
                expected_metadata={"family": selection.family, "target": reference.target,
                    **({"activation_role": "scanner_ranking_only"} if selection.mode == "manifest" else {})})
            if (metadata.get("family") != selection.family or metadata.get("target") != reference.target
                    or (selection.mode == "manifest" and (
                        metadata.get("activation_role") != "scanner_ranking_only"
                        or metadata.get("model_sha256") != reference.model_sha256))):
                model, features, metadata = None, [], {}
        metadata = deepcopy(metadata)
        if model is not None:
            metadata.setdefault("_artifact_runtime", {}).update(mode=selection.mode,
                manifest_sha256=selection.manifest_sha256, version=reference.version)
        snapshots[reference.target] = model, tuple(features), metadata
    after = {reference.target: _artifact_signature(reference.path) for reference in selection.references}
    # A newly created flat peer also invalidates the capture. No fallback to a
    # mixed selection and no retry that slides the observation within a decision.
    stable = selection.mode != "unavailable" and before == after
    if selection.mode == "legacy_flat":
        directory = PROJECT_ROOT / "ml" / "models" / selection.family
        try:
            stable &= {p.stem for p in directory.glob("*.pkl") if _SAFE_NAME.fullmatch(p.stem)} == set(before)
        except OSError:
            stable = False
    if not stable:
        snapshots = {target: (None, (), {}) for target in snapshots}
    return selection, snapshots, stable


def _family_snapshots(family: str, *, require_temporal_validation: bool):
    return scoped_value(("family_snapshots", str(PROJECT_ROOT), family, require_temporal_validation),
        lambda: _capture_family(_family_selection(family), require_temporal_validation=require_temporal_validation))


def _load_family(family: str, target: str, *, require_temporal_validation: bool):
    selection, snapshots, _ = _family_snapshots(family, require_temporal_validation=require_temporal_validation)
    model, features, metadata = snapshots.get(target, (None, (), {}))
    return _selected_path(selection, target), model, list(features), deepcopy(metadata)


def family_model_selection(family: str, *, targets: list[str] | None = None) -> dict[str, Any]:
    """Detached non-predictor provenance; never implies buy or exit permission."""
    selection, snapshots, stable = _family_snapshots(family, require_temporal_validation=True)
    wanted = set(targets) if targets is not None else set(snapshots)
    references = {reference.target: reference for reference in selection.references}
    heads = {}
    for target in sorted(wanted):
        model, _, metadata = snapshots.get(target, (None, (), {}))
        reference = references.get(target)
        heads[target] = {"status": "checked_artifact" if model is not None else "unknown",
                         "model_sha256": metadata.get("model_sha256") if model is not None else None,
                         "metadata_sha256": (metadata.get("_artifact_runtime") or {}).get("metadata_sha256") if model is not None else None,
                         "version": reference.version if reference is not None and reference.valid and selection.mode == "manifest" else None}
    return {"mode": selection.mode, "manifest_sha256": selection.manifest_sha256,
            "capture_stable": stable, "heads": heads, "role": "advisory_provenance_only",
            "buy_permission": False, "same_training_cohort_asserted": False}


def _load_unscoped(path: Path, *, require_temporal_validation: bool,
                   expected_model_sha256: str | None = None, expected_metadata_sha256: str | None = None,
                   expected_metadata: dict[str, Any] | None = None):
    meta_path = path.with_suffix(".meta.json")
    key = (str(path), require_temporal_validation, expected_model_sha256, expected_metadata_sha256,
           tuple(sorted((expected_metadata or {}).items())))
    try:
        model_stat, meta_stat = path.stat(), meta_path.stat()
        payload, metadata_payload = path.read_bytes(), meta_path.read_bytes()
    except OSError:
        with _lock:
            _cache.pop(key, None)
        return None, [], {}
    model_digest, metadata_digest = sha256(payload).hexdigest(), sha256(metadata_payload).hexdigest()
    signature = (model_stat.st_mtime_ns, model_stat.st_size, meta_stat.st_mtime_ns, meta_stat.st_size,
                 model_digest, metadata_digest)
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1], cached[2], cached[3]
        model, features, metadata = None, [], {}
        try:
            if expected_metadata_sha256 is not None and metadata_digest != expected_metadata_sha256:
                raise ValueError("metadata differs from the frozen manifest approval")
            metadata = json.loads(metadata_payload)
            if not isinstance(metadata, dict):
                raise ValueError("model metadata must be an object")
            if any(metadata.get(name) != value for name, value in (expected_metadata or {}).items()):
                raise ValueError("model identity differs from selected head")
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
            if expected_model_sha256 is not None and expected_hash != expected_model_sha256:
                raise ValueError("model differs from the frozen manifest approval")
            if not expected_hash or model_digest != expected_hash:
                raise ValueError("model/metadata checksum mismatch")
            features = metadata.get("features")
            if (not isinstance(features, list) or not features or len(set(features)) != len(features)
                    or any(name not in ALLOWED_FEATURES for name in features)):
                raise ValueError("model feature schema is absent")
            if (not checked_context_schema(metadata, features) or not checked_numeric_schema(metadata, features)
                    or not checked_semantics_schema(metadata, features)):
                raise ValueError("unproved specialized context encoding")
            if (metadata.get("family") == "exit" and metadata.get("target") == "best_exit_profile"
                    and not checked_exit_metadata(metadata)):
                raise ValueError("unsupported diagnostic exit classification")
            model = joblib.load(io.BytesIO(payload))
            metadata["_artifact_runtime"] = {"model_sha256": model_digest,
                                             "metadata_sha256": metadata_digest}
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
    value = _predict_snapshot_value(path, model, features, metadata, vec, expected_metadata=expected_metadata)
    identity = {**metadata, **(expected_metadata or {})}
    record_model_query(vec, family=identity.get("family", "unknown"), target=identity.get("target", "unknown"),
        operation="probability" if hasattr(model, "predict_proba") else "regression" if model is not None else "prediction", value=value,
        model=model, features=features, metadata=metadata)
    return value


def _predict_snapshot_value(path, model, features, metadata, vec, *, expected_metadata=None):
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
    path, model, features, metadata = _load_family(family, target,
        require_temporal_validation=require_temporal_validation)
    return _predict_snapshot(path, model, features, metadata, vec,
                             expected_metadata={"family": family, "target": target})


def predict_regression_estimate(family: str, target: str, vec: Any) -> dict[str, Any]:
    # Read one verified snapshot. A concurrent retrain must not combine an old
    # point prediction with a new model's error metadata.
    path, model, features, metadata = _load_family(family, target, require_temporal_validation=True)
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


def predict_diagnostic_exit_label(vec: Any) -> str | None:
    path, model, features, metadata = _load_family("exit", "best_exit_profile", require_temporal_validation=True)
    value = _diagnostic_exit_snapshot(path, model, features, metadata, vec)
    record_model_query(vec, family="exit", target="best_exit_profile", operation="diagnostic_label", value=value,
                       model=model, features=features, metadata=metadata)
    return value


def _diagnostic_exit_snapshot(path, model, features, metadata, vec):
    if model is None or not checked_exit_metadata(metadata):
        return None
    try:
        if list(getattr(model, "classes_", [])) != metadata["classes"]:
            return None
        matrix = coerce_feature_frame(checked_model_frame(input_frame(vec), features), features)
        prediction = scoped_prediction(("diagnostic_exit_label", str(path), id(model), metadata["model_sha256"]),
            matrix, lambda: model.predict(matrix))
        values = np.asarray(prediction)
        if values.shape != (1,) or not isinstance(values[0], str) or values[0] not in metadata["classes"]:
            return None
        return str(values[0])
    except Exception:
        return None


def predict_ranking_score(family: str, target: str, vec: Any) -> float | None:
    """Validated rank percentile (0-100), explicitly not an event probability."""
    path, model, features, metadata = _load_family(family, target, require_temporal_validation=True)
    value = _ranking_snapshot(path, model, features, metadata, vec, family=family, target=target)
    record_model_query(vec, family=family, target=target, operation="ranking_percentile", value=value,
                       model=model, features=features, metadata=metadata)
    return value


def predict_ranking_scores(family: str, target: str, vectors: list[Any]) -> list[float | None]:
    """Bounded vectorized advisory ranks from the same checked family snapshot.

    Bad row receipts stay unknown independently. No new acceptance rule,
    probability interpretation or buy permission is introduced by batching.
    """
    if not isinstance(vectors, list) or len(vectors) > 1000:
        raise ValueError("Ranking batch must be a list of at most 1000 vectors")
    if not vectors:
        return []
    _, model, features, metadata = _load_family(family, target, require_temporal_validation=True)
    values = [None] * len(vectors)
    if (model is not None and metadata.get("ranking_validation_ready") is True
            and metadata.get("ranking_metric_version") == RANKING_METRIC_VERSION
            and ranking_token_skill_ready(metadata.get("ranking_token_skill"))
            and (not financial_target(family, target) or supported_financial_training(metadata))):
        try:
            reference = np.asarray(metadata.get("rank_reference_quantiles") or [], dtype=float)
            if len(reference) < 2 or not np.isfinite(reference).all() or np.any(np.diff(reference) < 0):
                raise ValueError("Invalid rank reference")
            from features.auxiliary_semantics import PROOF_COLUMN
            positions, records = [], []
            for index, vector in enumerate(vectors):
                try:
                    if isinstance(vector, pd.Series):
                        row = vector.to_dict()
                        proof = vector.attrs.get(PROOF_COLUMN)
                        if proof is not None:
                            row[PROOF_COLUMN] = proof
                    elif isinstance(vector, dict):
                        row = dict(vector)
                    else:
                        frame = input_frame(vector)
                        if len(frame) != 1:
                            continue
                        row = frame.iloc[0].to_dict()
                    records.append(row)
                    positions.append(index)
                except (ValueError, TypeError, KeyError, RuntimeError, OverflowError):
                    continue
            if records:
                original = pd.DataFrame.from_records(records)
                try:
                    checked = checked_model_frame(original, features)
                except (ValueError, TypeError, KeyError, RuntimeError, OverflowError):
                    # A failed semantic receipt must not quarantine valid peers.
                    frames, valid_positions = [], []
                    for position, row in zip(positions, records):
                        try:
                            frames.append(checked_model_frame(pd.DataFrame([row]), features))
                            valid_positions.append(position)
                        except (ValueError, TypeError, KeyError, RuntimeError, OverflowError):
                            continue
                    positions = valid_positions
                    checked = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
                if not positions:
                    raise ValueError("No checked ranking inputs")
                matrix = coerce_feature_frame(checked, features)
                raw = np.asarray(scoped_prediction(("ranking_batch", id(model), metadata.get("model_sha256")),
                    matrix, lambda: model.rank_score(matrix)), dtype=float)
                if raw.shape != (len(positions),):
                    raise ValueError("Ranking batch output shape mismatch")
                for index, value in zip(positions, raw):
                    if np.isfinite(value):
                        low = np.searchsorted(reference, value, side="left")
                        high = np.searchsorted(reference, value, side="right")
                        values[index] = float(min(100., max(0., (low + high) / 2 / len(reference) * 100)))
        except Exception:
            values = [None] * len(vectors)
    for vector, value in zip(vectors, values):
        record_model_query(vector, family=family, target=target, operation="ranking_percentile", value=value,
            model=model, features=features, metadata=metadata)
    return values


def _ranking_snapshot(path, model, features, metadata, vec, *, family, target):
    if (model is None or metadata.get("ranking_validation_ready") is not True
            or metadata.get("ranking_metric_version") != RANKING_METRIC_VERSION
            or not ranking_token_skill_ready(metadata.get("ranking_token_skill"))):
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
        return float(min(100.0, max(0.0, (low + high) / 2 / len(reference) * 100)))
    except Exception:
        return None


__all__ = ["predict_model", "predict_artifact", "predict_regression_estimate", "predict_ranking_score", "predict_ranking_scores",
           "invalidate_model_cache", "family_model_selection", "predict_diagnostic_exit_label"]
