# analytics/ai_predict.py
"""
Inferencia en tiempo real para MemeBot 3.

•  Carga «ml/model.pkl» (LightGBM / sklearn) y la lista de *features*
   guardada en «ml/model.meta.json».
•  Expone:
       should_buy(vec)  →  probabilidad 0-1
       reload_model()   →  fuerza recarga en caliente
•  Usa un snapshot coherente de modelo y metadata con checksum y población
   financiera neta comprobada. Sin evidencia, la predicción es desconocida.
•  Convierte dict / Series / DataFrame con el contrato común de features.

Nota: Este archivo ahora usa logging en vez de print para integrarse con
el sistema de logs del proyecto (utils/logger.py).
"""

from __future__ import annotations

import json
import logging
import threading
import io
import copy
from hashlib import sha256
from pathlib import Path
from typing import Any, Optional, Sequence

import joblib
import numpy as np
import pandas as pd

from config.config import CFG, PROJECT_ROOT
from ml.feature_matrix import coerce_feature_frame
from ml.financial_targets import supported_financial_training
from features.builder import ALLOWED_FEATURES
from analytics.inference_scope import scoped_snapshot, scoped_prediction
from features.context_encoding import checked_context_schema
from features.numeric_encoding import checked_numeric_schema
from features.auxiliary_semantics import checked_semantics_schema, checked_model_frame, input_frame
from ml.entry_probability import supported_entry_probability, supported_entry_model
from ml.primary_activation import selected_reference, read_bundle

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
_TRAIN_STATUS_PATH: Path = (PROJECT_ROOT / "data" / "metrics" / "train_status.json").resolve()
_THRESHOLDS_BY_LANE_PATH: Path = (PROJECT_ROOT / "data" / "metrics" / "recommended_thresholds.by_lane.json").resolve()
_LEGACY_THRESHOLD_PATH: Path = (PROJECT_ROOT / "data" / "metrics" / "recommended_threshold.json").resolve()
_REGISTRY_PATH = PROJECT_ROOT / "ml" / "model_registry.json"
_MODELS_DIR = PROJECT_ROOT / "ml" / "models"

# ──────────────────── estado global ───────────────────────────
_model_lock = threading.Lock()
_model: Optional[Any] = None               # objeto LightGBM / sklearn
_model_mtime: Optional[float] = None       # timestamp del .pkl
_model_path_loaded: Optional[Path] = None
_model_signature: tuple | None = None
_loaded_meta: dict[str, Any] = {}
_FEATURES: Optional[Sequence[str]] = None  # orden de columnas
_meta_cache: Optional[dict[str, Any]] = None
_meta_mtime: Optional[float] = None
_meta_path_loaded: Optional[Path] = None


# ╭────────────────── helpers internos ─────────────────╮
def _effective_model_paths() -> tuple[Path, Path, bool]:
    # Candidates are research artifacts, never runtime authority by mtime.
    try:
        selected = selected_reference(_REGISTRY_PATH, _MODELS_DIR, _MODEL_PATH)
        if selected is not None:
            return selected["paths"]["model.pkl"], selected["paths"]["model.meta.json"], False
    except Exception:
        pass  # Diagnostic paths only; loader fails closed on selector errors.
    return _MODEL_PATH, _META_PATH, False


def _load_model_unscoped():
    """One checksum-checked net-model snapshot; absence is neutral/unknown."""
    global _model, _model_mtime, _model_path_loaded, _FEATURES, _model_signature, _loaded_meta
    with _model_lock:
        try:
            selected = selected_reference(_REGISTRY_PATH, _MODELS_DIR, _MODEL_PATH)
            model_path = selected["paths"]["model.pkl"] if selected else _MODEL_PATH
            meta_path = selected["paths"]["model.meta.json"] if selected else _META_PATH
            model_stat, meta_stat = model_path.stat(), meta_path.stat()
            captured = ({name: path.read_bytes() for name, path in selected["paths"].items()}
                        if selected else {"model.pkl": model_path.read_bytes(), "model.meta.json": meta_path.read_bytes()})
            content_hashes = {name: sha256(payload).hexdigest() for name, payload in captured.items()}
            signature = (str(model_path), str(meta_path), model_stat.st_mtime_ns, model_stat.st_size,
                         meta_stat.st_mtime_ns, meta_stat.st_size, tuple(sorted(content_hashes.items())))
            if selected:
                signature += (selected["revision"], json.dumps(selected["reference"], sort_keys=True),
                    tuple((name, path.stat().st_mtime_ns, path.stat().st_size)
                          for name, path in sorted(selected["paths"].items())))
        except Exception:
            signature = None
        if signature is not None and signature == _model_signature:
            return _model, list(_FEATURES or []), copy.deepcopy(_loaded_meta)
        _model, _FEATURES, _loaded_meta = None, None, {}
        _model_mtime, _model_path_loaded, _model_signature = None, None, signature
        if signature is None:
            return None, [], {}
        try:
            if selected:
                _model, metadata, documents, _ = read_bundle(selected["reference"], _REGISTRY_PATH, _MODELS_DIR,
                    captured_payloads=captured)
            else:
                metadata = json.loads(captured["model.meta.json"])
            if not isinstance(metadata, dict):
                raise ValueError("model metadata must be an object")
            metadata["_primary_runtime"] = {"revision": selected["revision"] if selected else None,
                "model_path": str(model_path), "meta_path": str(meta_path),
                "acceptance": documents["acceptance.json"] if selected else None,
                "mode": "atomic_primary_bundle" if selected else "checked_legacy_artifact",
                "component_sha256": content_hashes}
            _loaded_meta = metadata
            if not supported_financial_training(metadata, entry=True):
                return None, [], copy.deepcopy(metadata)
            if (metadata.get("validation_split") or {}).get("label_availability_purged") is not True:
                return None, [], copy.deepcopy(metadata)
            features = metadata.get("features")
            if (not isinstance(features, list) or not features or len(set(features)) != len(features)
                    or any(feature not in ALLOWED_FEATURES for feature in features)):
                raise ValueError("unproved entry feature schema")
            if (not checked_context_schema(metadata, features) or not checked_numeric_schema(metadata, features)
                    or not checked_semantics_schema(metadata, features)):
                raise ValueError("unproved entry context encoding")
            payload = captured["model.pkl"]
            if sha256(payload).hexdigest() != metadata.get("model_sha256"):
                raise ValueError("model/metadata checksum mismatch")
            if not supported_entry_probability(metadata):
                raise ValueError("unproved primary calibrated probability")
            # Deserialize precisely the bytes that passed the checksum, never
            # a replacement path from a concurrent promotion.
            if not selected:
                _model = joblib.load(io.BytesIO(payload))
            if not supported_entry_model(_model, metadata):
                raise ValueError("primary probability model/calibration mismatch")
            _FEATURES = list(features)
            _model_mtime, _model_path_loaded = model_stat.st_mtime, model_path
        except Exception as exc:
            _model, _FEATURES = None, None
            log.warning("Entry model unavailable: %s", type(exc).__name__)
        return _model, list(_FEATURES or []), copy.deepcopy(_loaded_meta)


def _load_model():
    return scoped_snapshot(("primary_entry", str(_MODEL_PATH)), _load_model_unscoped)


def _load_meta() -> dict[str, Any]:
    return copy.deepcopy(_load_model()[2])


def _load_train_status() -> dict[str, Any]:
    if not _TRAIN_STATUS_PATH.exists():
        return {}
    try:
        payload = json.loads(_TRAIN_STATUS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("No se pudo leer train_status %s: %s", _TRAIN_STATUS_PATH, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("No se pudo leer %s: %s", path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


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

    return coerce_feature_frame(X, _FEATURES)


# ╭────────────────── API pública ─────────────────╮
def should_buy(vec: Any) -> float | None:
    """
    Devuelve la probabilidad de compra (label = 1) para el vector de características.
    •  `vec` puede ser dict, pandas.Series o pandas.DataFrame (1 fila).
    •  Sin un modelo neto comprobado, devuelve None (desconocido, no fracaso).
    """
    model, features, _metadata = _load_model()
    if model is None:
        return None
    try:
        X = coerce_feature_frame(checked_model_frame(input_frame(vec), features), features)
        if hasattr(model, "predict_proba"):
            classes = list(model.classes_)
            if classes != [0, 1]:
                return None
            proba = scoped_prediction(("entry_probability", id(model), _metadata.get("model_sha256")),
                                      X, lambda: model.predict_proba(X)[0, 1])
        else:
            proba = scoped_prediction(("entry_prediction", id(model), _metadata.get("model_sha256")),
                                      X, lambda: model.predict(X)[0])
        value = float(proba)
        return value if np.isfinite(value) and 0 <= value <= 1 else None
    except Exception as exc:
        log.debug("Entry prediction unknown: %s", type(exc).__name__)
        return None


def entry_prediction_state() -> dict[str, Any]:
    """Acceptance and thresholds from the same model snapshot as should_buy."""
    model, _features, metadata = _load_model()
    checked = model is not None and supported_financial_training(metadata, entry=True)
    return {"activation_ready": bool(checked and metadata.get("activation_ready") is True),
            "metadata": copy.deepcopy(metadata) if checked else {}}


def primary_model_selection() -> dict[str, Any]:
    """Original consumed snapshot provenance, never a strategy/buy approval."""
    model, features, metadata = _load_model()
    selected = metadata.get("_primary_runtime") or {}
    checked = model is not None and bool(selected.get("component_sha256"))
    return {"status": "checked_artifact" if checked else "unknown",
        "mode": selected.get("mode") if checked else "unavailable",
        "model_id": metadata.get("artifact_model_id") if checked else None,
        "revision": selected.get("revision") if checked else None,
        "component_sha256": copy.deepcopy(selected["component_sha256"]) if checked else {},
        "feature_schema_sha256": sha256(json.dumps(features, separators=(",", ":")).encode()).hexdigest() if checked else None,
        "role": "decision_provenance_only", "buy_permission": False,
        "full_strategy_profitability_established": False}


def reload_model() -> None:
    """Borra el modelo en memoria para forzar recarga (p. ej. tras retrain)."""
    global _model, _model_mtime, _model_path_loaded, _meta_cache, _meta_mtime, _meta_path_loaded, _model_signature
    with _model_lock:
        _model = None
        _model_mtime = None
        _model_path_loaded = None
        _model_signature = None
        _meta_cache = None
        _meta_mtime = None
        _meta_path_loaded = None
    _load_model()
    log.info("🔄 Modelo recargado manualmente (forzando reload en memoria)")

def model_runtime_status() -> dict[str, Any]:
    """Estado ligero del modelo y de su activación recomendada."""
    model, features, meta = _load_model()
    train_status = _load_train_status()
    dataset_quality = meta.get("dataset_quality")
    if not isinstance(dataset_quality, dict):
        dataset_quality = train_status.get("dataset_quality")
    dataset_quality_passed = meta.get("dataset_quality_passed")
    if dataset_quality_passed is None and isinstance(dataset_quality, dict):
        dataset_quality_passed = dataset_quality.get("passed")
    eligible_rows = train_status.get("eligible_rows")
    if eligible_rows is None and isinstance(dataset_quality, dict):
        eligible_rows = dataset_quality.get("rows")
    eligible_unique_tokens = train_status.get("eligible_unique_tokens")
    if eligible_unique_tokens is None and isinstance(dataset_quality, dict):
        eligible_unique_tokens = dataset_quality.get("unique_tokens")
    eligible_positives = train_status.get("eligible_positives")
    if eligible_positives is None and isinstance(dataset_quality, dict):
        eligible_positives = dataset_quality.get("positives")
    holdout_rows = train_status.get("holdout_rows")
    if holdout_rows is None and isinstance(dataset_quality, dict):
        holdout_rows = dataset_quality.get("holdout_rows")
    skip_reasons = train_status.get("skip_reasons")
    if skip_reasons is None and isinstance(dataset_quality, dict):
        skip_reasons = dataset_quality.get("reasons")

    rows_to_next_model = train_status.get("rows_to_next_model")
    positives_to_next_model = train_status.get("positives_to_next_model")
    unique_tokens_to_next_model = train_status.get("unique_tokens_to_next_model")
    holdout_rows_to_next_model = train_status.get("holdout_rows_to_next_model")
    holdout_positives_to_next_model = train_status.get("holdout_positives_to_next_model")
    if rows_to_next_model is None and eligible_rows is not None and eligible_unique_tokens is not None:
        rows_to_next_model = max(
            max(0, int(getattr(CFG, "ML_MIN_DATASET_ROWS", 190) or 190) - int(eligible_rows)),
            max(0, int(getattr(CFG, "ML_MIN_UNIQUE_TOKENS", 190) or 190) - int(eligible_unique_tokens)),
        )
    if positives_to_next_model is None and eligible_positives is not None:
        positives_to_next_model = max(0, int(getattr(CFG, "ML_MIN_POSITIVES", 40) or 40) - int(eligible_positives))
    if unique_tokens_to_next_model is None and eligible_unique_tokens is not None:
        unique_tokens_to_next_model = max(
            0,
            int(getattr(CFG, "ML_MIN_UNIQUE_TOKENS", 190) or 190) - int(eligible_unique_tokens),
        )
    if holdout_rows_to_next_model is None and holdout_rows is not None:
        holdout_rows_to_next_model = max(
            0,
            int(getattr(CFG, "ML_MIN_HOLDOUT_ROWS", 40) or 40) - int(holdout_rows),
        )
    holdout_positives = train_status.get("holdout_positives")
    if holdout_positives is None and isinstance(dataset_quality, dict):
        holdout_positives = dataset_quality.get("holdout_positives")
    if holdout_positives_to_next_model is None and holdout_positives is not None:
        holdout_positives_to_next_model = max(
            0,
            int(getattr(CFG, "ML_MIN_HOLDOUT_POSITIVES", 8) or 8) - int(holdout_positives),
        )
    blocker = train_status.get("blocker")
    if blocker is None and skip_reasons:
        blocker = ",".join(str(item) for item in skip_reasons if str(item))
    effective_model_path, effective_meta_path, candidate_fallback = _effective_model_paths()
    selection = meta.get("_primary_runtime") or {}
    if selection:
        effective_model_path, effective_meta_path = Path(selection["model_path"]), Path(selection["meta_path"])
    return {
        "primary_selection_revision": selection.get("revision"),
        "primary_champion_acceptance": selection.get("acceptance"),
        "threshold_result": copy.deepcopy(meta.get("threshold_result")),
        "model_exists": effective_model_path.exists(),
        "meta_exists": effective_meta_path.exists(),
        "active_model_exists": _MODEL_PATH.exists(),
        "active_meta_exists": _META_PATH.exists(),
        "candidate_fallback_used": bool(candidate_fallback),
        "candidate_model_path": str(effective_model_path) if candidate_fallback else None,
        "candidate_meta_path": str(effective_meta_path) if candidate_fallback else None,
        "model_loaded": model is not None,
        "features_count": len(features),
        "activation_ready": bool(meta.get("activation_ready") is True and model is not None
                                 and supported_financial_training(meta, entry=True)),
        "financial_training": meta.get("financial_training"),
        "financial_training_ready": supported_financial_training(meta, entry=True),
        "probability_validation_ready": bool(model is not None and supported_entry_probability(meta)),
        "probability_contract_version": meta.get("probability_contract_version"),
        "probability_evaluation": meta.get("probability_evaluation"),
        "dataset_quality_passed": dataset_quality_passed,
        "threshold_metric": meta.get("threshold_metric") or train_status.get("threshold_metric"),
        "training_scope": meta.get("training_scope") or train_status.get("training_scope"),
        "bootstrap_used": meta.get("bootstrap_used") if meta.get("bootstrap_used") is not None else train_status.get("bootstrap_used"),
        "strict_productive_dataset": train_status.get("strict_productive_dataset") or meta.get("strict_productive_dataset"),
        "bootstrap_candidate_dataset": train_status.get("bootstrap_candidate_dataset") or meta.get("bootstrap_candidate_dataset"),
        "rows": meta.get("rows") or train_status.get("rows") or eligible_rows,
        "eligible_rows": eligible_rows,
        "eligible_unique_tokens": eligible_unique_tokens,
        "eligible_positives": eligible_positives,
        "holdout_rows": holdout_rows,
        "rows_missing_lane_metadata": train_status.get("rows_missing_lane_metadata"),
        "last_train_attempt_at": train_status.get("last_train_attempt_at"),
        "last_train_status": train_status.get("last_train_status") or train_status.get("status"),
        "skip_reasons": skip_reasons,
        "rows_to_next_model": rows_to_next_model,
        "positives_to_next_model": positives_to_next_model,
        "unique_tokens_to_next_model": unique_tokens_to_next_model,
        "holdout_rows_to_next_model": holdout_rows_to_next_model,
        "holdout_positives_to_next_model": holdout_positives_to_next_model,
        "blocker": blocker,
        "model_path": str(effective_model_path),
        "meta_path": str(effective_meta_path),
        "active_model_path": str(_MODEL_PATH),
        "active_meta_path": str(_META_PATH),
        "train_status_path": str(_TRAIN_STATUS_PATH),
    }


def threshold_runtime_metadata() -> dict[str, Any]:
    """Threshold metadata with by-lane support and legacy fallback."""
    meta = _load_model()[2]
    if (meta.get("_primary_runtime") or {}).get("mode") == "atomic_primary_bundle":
        by_lane = meta.get("thresholds_by_lane") or {}
        return {"source": "atomic_primary_bundle", "path": meta["_primary_runtime"]["meta_path"],
            "global": by_lane.get("global") or {"threshold": (meta.get("threshold_result") or {}).get("picked"),
                "activation_ready": meta.get("activation_ready") is True},
            "by_lane": by_lane.get("by_lane") or {}, "revision": meta["_primary_runtime"]["revision"]}
    try:
        if selected_reference(_REGISTRY_PATH, _MODELS_DIR, _MODEL_PATH) is not None:
            return {"source": "unavailable_atomic_primary_bundle", "path": str(_REGISTRY_PATH),
                    "global": {}, "by_lane": {}}
    except Exception:
        return {"source": "invalid_primary_selector", "path": str(_REGISTRY_PATH), "global": {}, "by_lane": {}}
    by_lane = _load_json_file(_THRESHOLDS_BY_LANE_PATH)
    if by_lane:
        return {
            "source": "by_lane",
            "path": str(_THRESHOLDS_BY_LANE_PATH),
            "global": by_lane.get("global") or {},
            "by_lane": by_lane.get("by_lane") or {},
        }
    legacy = _load_json_file(_LEGACY_THRESHOLD_PATH)
    return {
        "source": "legacy",
        "path": str(_LEGACY_THRESHOLD_PATH),
        "global": {
            "threshold": legacy.get("picked"),
            "activation_ready": legacy.get("activation_ready"),
            "mode_recommended": "shadow" if not legacy.get("activation_ready") else "enforce",
            "reason": legacy.get("activation_reason"),
        },
        "by_lane": {},
    }


__all__ = ["should_buy", "entry_prediction_state", "primary_model_selection", "reload_model", "model_runtime_status", "threshold_runtime_metadata"]
