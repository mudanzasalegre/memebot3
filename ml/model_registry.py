from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib

from config.config import CFG, PROJECT_ROOT


MODELS_DIR = PROJECT_ROOT / "ml" / "models"
REGISTRY_PATH = PROJECT_ROOT / "ml" / "model_registry.json"


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
    return f"{stamp}_{name}"


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_bytes(path, json.dumps(payload, indent=2, default=str).encode("utf-8"))


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
    candidate_dir = (MODELS_DIR / str(family) / model_id) if family else (MODELS_DIR / model_id)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    model_path = candidate_dir / "model.pkl"
    meta_path = candidate_dir / "model.meta.json"
    tmp_model = model_path.with_name(model_path.name + ".tmp")
    joblib.dump(model, tmp_model)
    os.replace(tmp_model, model_path)
    atomic_write_json(meta_path, meta)
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
    if not REGISTRY_PATH.exists():
        return {}
    try:
        payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _ensure_promotion_unlocked() -> None:
    if bool(getattr(CFG, "STRATEGY_OPTIMIZATION_LOCK", True)):
        raise RuntimeError("STRATEGY_OPTIMIZATION_LOCK=true blocks model promotion")


def promote_candidate(artifact: ModelArtifactSet, *, active_model_path: Path | None = None) -> dict[str, Any]:
    _ensure_promotion_unlocked()
    active_model_path = active_model_path or CFG.MODEL_PATH
    active_meta_path = active_model_path.with_suffix(".meta.json")
    if not artifact.model_path.exists() or not artifact.meta_path.exists():
        raise FileNotFoundError("candidate model/meta is incomplete")
    # Validate load and JSON before touching active files.
    joblib.load(artifact.model_path)
    json.loads(artifact.meta_path.read_text(encoding="utf-8"))

    registry = _load_registry()
    previous = registry.get("active_model_id")
    tmp_model = active_model_path.with_name(active_model_path.name + ".tmp")
    tmp_meta = active_meta_path.with_name(active_meta_path.name + ".tmp")
    active_model_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(artifact.model_path, tmp_model)
    shutil.copy2(artifact.meta_path, tmp_meta)
    os.replace(tmp_model, active_model_path)
    os.replace(tmp_meta, active_meta_path)
    if artifact.thresholds_path and artifact.thresholds_path.exists():
        target = PROJECT_ROOT / "data" / "metrics" / "recommended_thresholds.by_lane.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        shutil.copy2(artifact.thresholds_path, tmp)
        os.replace(tmp, target)

    new_registry = {
        "active_model_id": artifact.model_id,
        "previous_model_id": previous,
        "active_since_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "feature_set_hash": json.loads(artifact.meta_path.read_text(encoding="utf-8")).get("feature_set_hash"),
        "status": "active",
    }
    atomic_write_json(REGISTRY_PATH, new_registry)
    return new_registry


def promote_family_candidate(
    artifact: ModelArtifactSet,
    *,
    family: str,
    active_name: str = "active_model.pkl",
) -> dict[str, Any]:
    _ensure_promotion_unlocked()
    family_dir = MODELS_DIR / str(family)
    active_model_path = family_dir / active_name
    registry = _load_registry()
    families = dict(registry.get("families") or {})
    joblib.load(artifact.model_path)
    meta = json.loads(artifact.meta_path.read_text(encoding="utf-8"))
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


__all__ = [
    "ModelArtifactSet",
    "MODELS_DIR",
    "REGISTRY_PATH",
    "utc_model_id",
    "atomic_write_json",
    "write_candidate",
    "promote_candidate",
    "promote_family_candidate",
]
