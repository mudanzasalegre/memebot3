from __future__ import annotations

import json
import os
import shutil
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import joblib
from hashlib import sha256

from config.config import CFG, PROJECT_ROOT
from ml.financial_targets import supported_financial_training, financial_target
from features.context_encoding import checked_context_schema
from features.numeric_encoding import checked_numeric_schema
from ml.entry_probability import supported_entry_probability, supported_entry_model
from ml.primary_activation import (registry_lock, read_registry, active_epoch, write_bundle, read_bundle,
    selected_reference, legacy_archive, commit_selection, refresh_legacy_mirrors)
from utils.atomic_json import write_json_atomic


MODELS_DIR = PROJECT_ROOT / "ml" / "models"
REGISTRY_PATH = PROJECT_ROOT / "ml" / "model_registry.json"
ACTIVATION_READY_PROMOTION_ERROR = "activation_ready=true required for model promotion"


@dataclass(frozen=True)
class ModelArtifactSet:
    model_id: str
    model_path: Path
    meta_path: Path
    thresholds_path: Path | None = None
    val_preds_path: Path | None = None
    segment_report_path: Path | None = None


def utc_model_id(name: str = "model") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{name}_{uuid4().hex[:10]}"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    write_json_atomic(path, payload)


def write_candidate(
    *,
    model: Any,
    meta: dict[str, Any],
    model_id: str | None = None,
    family: str | None = None,
    thresholds: dict[str, Any] | None = None,
    val_preds_path: Path | None = None,
    segment_report_path: Path | None = None,
) -> ModelArtifactSet:
    model_id = model_id or utc_model_id(str(meta.get("selected_model_name") or "model"))
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)
           for value in ([model_id, str(family)] if family else [model_id])):
        raise ValueError("Candidate identity must be a single safe path component")
    candidate_dir = (MODELS_DIR / str(family) / model_id) if family else (MODELS_DIR / model_id)
    if not candidate_dir.resolve().is_relative_to(MODELS_DIR.resolve()):
        raise ValueError("Candidate path escapes model store")
    candidate_dir.mkdir(parents=True, exist_ok=False)
    model_path = candidate_dir / "model.pkl"
    meta_path = candidate_dir / "model.meta.json"
    tmp_model = model_path.with_name(model_path.name + ".tmp")
    joblib.dump(model, tmp_model)
    os.replace(tmp_model, model_path)
    payload = {**meta, "artifact_model_id": model_id, "model_sha256": sha256(model_path.read_bytes()).hexdigest()}
    if thresholds is not None:
        payload["thresholds_by_lane"] = thresholds
    atomic_write_json(meta_path, payload)
    thresholds_path = None
    if thresholds is not None:
        thresholds_path = candidate_dir / "thresholds.by_lane.json"
        atomic_write_json(thresholds_path, thresholds)
    if val_preds_path and val_preds_path.exists():
        shutil.copy2(val_preds_path, candidate_dir / "val_preds.csv")
    if segment_report_path and segment_report_path.exists():
        shutil.copy2(segment_report_path, candidate_dir / "segment_report.json")
    return ModelArtifactSet(model_id, model_path, meta_path, thresholds_path, val_preds_path, segment_report_path)


def _load_registry() -> dict[str, Any]:
    return read_registry(REGISTRY_PATH)


def _ensure_promotion_unlocked() -> None:
    if bool(getattr(CFG, "STRATEGY_OPTIMIZATION_LOCK", True)):
        raise RuntimeError("STRATEGY_OPTIMIZATION_LOCK=true blocks model promotion")


def _ensure_activation_ready(meta: dict[str, Any]) -> None:
    if meta.get("activation_ready") is not True:
        raise RuntimeError(ACTIVATION_READY_PROMOTION_ERROR)


def _ensure_financial_artifact(meta, path, *, entry=False, model_bytes=None):
    if not checked_context_schema(meta, meta.get("features") or []) or not checked_numeric_schema(meta, meta.get("features") or []):
        raise RuntimeError("checked T0 context encoding required for model promotion")
    if not supported_financial_training(meta, entry=entry):
        raise RuntimeError("checked net financial training required for model promotion")
    from features.builder import ALLOWED_FEATURES
    features = meta.get("features")
    if (not isinstance(features, list) or not features or len(set(features)) != len(features)
            or any(feature not in ALLOWED_FEATURES for feature in features)):
        raise RuntimeError("proved T0 feature schema required for model promotion")
    if entry:
        if (meta.get("validation_split") or {}).get("label_availability_purged") is not True:
            raise RuntimeError("purged financial label availability required for model promotion")
    if meta.get("model_sha256") != sha256(path.read_bytes() if model_bytes is None else model_bytes).hexdigest():
        raise RuntimeError("model/metadata checksum mismatch blocks promotion")
    if entry and not supported_entry_probability(meta):
        raise RuntimeError("checked temporal probability evidence required for model promotion")


def promote_candidate(artifact: ModelArtifactSet, *, active_model_path: Path | None = None,
                      approval: dict | None = None) -> dict[str, Any]:
    _ensure_promotion_unlocked()
    active_model_path = active_model_path or CFG.MODEL_PATH
    if not artifact.model_path.exists() or not artifact.meta_path.exists():
        raise FileNotFoundError("candidate model/meta is incomplete")
    model_bytes, meta_bytes = artifact.model_path.read_bytes(), artifact.meta_path.read_bytes()
    meta = json.loads(meta_bytes)
    if meta.get("artifact_model_id") != artifact.model_id:
        raise RuntimeError("Primary candidate identity/metadata mismatch")
    _ensure_activation_ready(meta)
    _ensure_financial_artifact(meta, artifact.model_path, entry=True, model_bytes=model_bytes)
    # Validate load and JSON before touching active files.
    candidate_model = joblib.load(io.BytesIO(model_bytes))
    if not supported_entry_model(candidate_model, meta):
        raise RuntimeError("checked primary probability model/calibration required for model promotion")

    from ml.primary_champion import supported_approval
    if not supported_approval(approval or {}, model_bytes, meta_bytes):
        raise RuntimeError("checked same-later-cohort approval required for primary promotion")
    with registry_lock(REGISTRY_PATH):
        registry = _load_registry()
        if approval["expected_active_epoch"] != active_epoch(registry, active_model_path):
            raise RuntimeError("Primary incumbent changed during comparison; preserve current selection")
        current = selected_reference(REGISTRY_PATH, MODELS_DIR, active_model_path)
        previous = current["reference"] if current else None
        if previous:
            read_bundle(previous, REGISTRY_PATH, MODELS_DIR)
        metrics = PROJECT_ROOT / "data" / "metrics"
        archived = (registry.get("primary_activation") or {}).get("legacy_archive")
        if current is None:
            archived = legacy_archive(active_model_path, registry_path=REGISTRY_PATH, models_dir=MODELS_DIR, metrics_dir=metrics)
        def encode(value):
            return json.dumps(value, sort_keys=True, allow_nan=False).encode()
        payloads = {"model.pkl": model_bytes, "model.meta.json": meta_bytes,
            "threshold.json": encode(meta.get("threshold_result") or {}),
            "thresholds.by_lane.json": encode(meta.get("thresholds_by_lane") or {}), "acceptance.json": encode(approval)}
        reference = write_bundle(payloads, registry_path=REGISTRY_PATH, models_dir=MODELS_DIR, model_id=artifact.model_id)
        read_bundle(reference, REGISTRY_PATH, MODELS_DIR)
        registry["feature_set_hash"] = meta.get("feature_set_hash")
        new_registry = commit_selection(registry, registry_path=REGISTRY_PATH, active=reference,
            previous=previous, model_alias=active_model_path, legacy=archived)
        try:
            warnings = refresh_legacy_mirrors(reference, registry_path=REGISTRY_PATH, models_dir=MODELS_DIR,
                model_alias=active_model_path, metrics_dir=metrics)
        except Exception as exc:
            warnings = ["mirror_export:" + type(exc).__name__]
        return {**new_registry, "mirror_errors": warnings}


def rollback_primary_candidate(*, active_model_path: Path | None = None) -> dict[str, Any]:
    _ensure_promotion_unlocked()
    alias = active_model_path or CFG.MODEL_PATH
    with registry_lock(REGISTRY_PATH):
        registry = _load_registry()
        selection = registry.get("primary_activation") or {}
        current = selected_reference(REGISTRY_PATH, MODELS_DIR, alias)
        previous = selection.get("previous")
        if current is None or not previous:
            raise RuntimeError("No checked previous primary bundle to restore")
        _, metadata, _, _ = read_bundle(previous, REGISTRY_PATH, MODELS_DIR)
        registry["feature_set_hash"] = metadata.get("feature_set_hash")
        restored = commit_selection(registry, registry_path=REGISTRY_PATH, active=previous,
            previous=current["reference"], model_alias=alias, legacy=selection.get("legacy_archive"))
        try:
            warnings = refresh_legacy_mirrors(previous, registry_path=REGISTRY_PATH, models_dir=MODELS_DIR,
                model_alias=alias, metrics_dir=PROJECT_ROOT / "data" / "metrics")
        except Exception as exc:
            warnings = ["mirror_export:" + type(exc).__name__]
        return {**restored, "mirror_errors": warnings}


def _promote_family_candidate_locked(
    artifact: ModelArtifactSet,
    *,
    family: str,
    active_name: str = "active_model.pkl",
) -> dict[str, Any]:
    _ensure_promotion_unlocked()
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)
           for value in (family, active_name)):
        raise ValueError("Family activation paths must be single safe components")
    family_dir = MODELS_DIR / str(family)
    active_model_path = family_dir / active_name
    if not active_model_path.resolve().is_relative_to(MODELS_DIR.resolve()):
        raise ValueError("Family activation escapes model store")
    registry = _load_registry()
    families = dict(registry.get("families") or {})
    meta = json.loads(artifact.meta_path.read_text(encoding="utf-8"))
    _ensure_activation_ready(meta)
    if financial_target(family, meta.get("target")):
        _ensure_financial_artifact(meta, artifact.model_path)
    elif not checked_context_schema(meta, meta.get("features") or []) or not checked_numeric_schema(meta, meta.get("features") or []):
        raise RuntimeError("checked T0 context encoding required for model promotion")
    joblib.load(artifact.model_path)
    family_dir.mkdir(parents=True, exist_ok=True)
    tmp_model = active_model_path.with_name(active_model_path.name + ".tmp")
    tmp_meta = active_model_path.with_suffix(".meta.json.tmp")
    shutil.copy2(artifact.model_path, tmp_model)
    shutil.copy2(artifact.meta_path, tmp_meta)
    os.replace(tmp_model, active_model_path)
    os.replace(tmp_meta, active_model_path.with_suffix(".meta.json"))
    families[str(family)] = {
        "active_model_id": artifact.model_id,
        "active_model_path": str(active_model_path),
        "active_since_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "feature_set_hash": meta.get("feature_set_hash"),
        "validation_metrics": meta.get("validation_metrics") or meta.get("metrics"),
        "status": "active",
    }
    registry["families"] = families
    atomic_write_json(REGISTRY_PATH, registry)
    return registry


def promote_family_candidate(artifact: ModelArtifactSet, *, family: str,
                             active_name: str = "active_model.pkl") -> dict[str, Any]:
    with registry_lock(REGISTRY_PATH):
        return _promote_family_candidate_locked(artifact, family=family, active_name=active_name)


__all__ = [
    "ModelArtifactSet",
    "MODELS_DIR",
    "REGISTRY_PATH",
    "ACTIVATION_READY_PROMOTION_ERROR",
    "utc_model_id",
    "atomic_write_json",
    "write_candidate",
    "promote_candidate",
    "promote_family_candidate",
    "rollback_primary_candidate",
]
