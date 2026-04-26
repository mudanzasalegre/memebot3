# analytics/ai_predict.py
"""
Inferencia en tiempo real para MemeBot 3.

•  Carga «ml/model.pkl» (LightGBM / sklearn) y la lista de *features*
   guardada en «ml/model.meta.json».
•  Expone:
       should_buy(vec)  →  probabilidad 0-1
       reload_model()   →  fuerza recarga en caliente
•  Convierte cualquier entrada (dict / Series / DataFrame) a un
   DataFrame de una fila con las columnas exactas que espera el modelo,
   convierte a numérico, llena NaN con 0 y hace la predicción.

Nota: Este archivo ahora usa logging en vez de print para integrarse con
el sistema de logs del proyecto (utils/logger.py).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

import joblib
import numpy as np
import pandas as pd

from config.config import CFG, PROJECT_ROOT
from ml.feature_matrix import coerce_feature_frame

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

# ──────────────────── estado global ───────────────────────────
_model_lock = threading.Lock()
_model: Optional[Any] = None               # objeto LightGBM / sklearn
_model_mtime: Optional[float] = None       # timestamp del .pkl
_FEATURES: Optional[Sequence[str]] = None  # orden de columnas
_meta_cache: Optional[dict[str, Any]] = None
_meta_mtime: Optional[float] = None


# ╭────────────────── helpers internos ─────────────────╮
def _load_model() -> None:
    """Carga modelo y lista de features en memoria (lazy, thread-safe)."""
    global _model, _model_mtime, _FEATURES

    if not _MODEL_PATH.exists():  # primera ejecución: aún no hay modelo
        _model = None
        _model_mtime = None
        _FEATURES = None
        log.debug("Modelo no encontrado en disco: %s", _MODEL_PATH)
        return

    mtime = _MODEL_PATH.stat().st_mtime
    if _model is not None and mtime == _model_mtime:
        # Ya actualizado en memoria
        return

    with _model_lock:
        # doble-check por concurrencia
        current_mtime = _MODEL_PATH.stat().st_mtime
        if _model is None or current_mtime != _model_mtime:
            _model = joblib.load(_MODEL_PATH)
            _model_mtime = current_mtime

            # lista de columnas entrenadas
            _FEATURES = None
            if _META_PATH.exists():
                try:
                    meta = json.loads(_META_PATH.read_text())
                    _FEATURES = meta.get("features")
                except Exception as e:
                    log.warning("No se pudo leer meta %s: %s", _META_PATH, e)

            # Fallback para algunos modelos (p.ej. LightGBM con atributo feature_name)
            if not _FEATURES:
                try:
                    _FEATURES = list(_model.feature_name())
                except Exception:
                    raise RuntimeError(
                        f"No se pudo determinar _FEATURES; falta {_META_PATH} "
                        "y el modelo no expone feature_name()."
                    )

            log.info("🧠 Modelo cargado: %s (mtime=%d)", _MODEL_PATH.name, int(_model_mtime))


def _load_meta() -> dict[str, Any]:
    global _meta_cache, _meta_mtime

    if not _META_PATH.exists():
        _meta_cache = {}
        _meta_mtime = None
        return {}

    mtime = _META_PATH.stat().st_mtime
    if _meta_cache is not None and _meta_mtime == mtime:
        return dict(_meta_cache)

    with _model_lock:
        current_mtime = _META_PATH.stat().st_mtime if _META_PATH.exists() else None
        if current_mtime is None:
            _meta_cache = {}
            _meta_mtime = None
            return {}
        if _meta_cache is None or _meta_mtime != current_mtime:
            try:
                _meta_cache = json.loads(_META_PATH.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                log.warning("No se pudo leer meta %s: %s", _META_PATH, exc)
                _meta_cache = {}
            _meta_mtime = current_mtime
    return dict(_meta_cache or {})


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
def should_buy(vec: Any) -> float:
    """
    Devuelve la probabilidad de compra (label = 1) para el vector de características.
    •  `vec` puede ser dict, pandas.Series o pandas.DataFrame (1 fila).
    •  Si no hay modelo aún (primera ejecución), devuelve 0.0.
    """
    _load_model()
    if _model is None:
        log.debug("Predicción omitida: no hay modelo aún, devolviendo 0.0")
        return 0.0  # primera ejecución: aún sin modelo entrenado

    X = _to_dataframe(vec)

    # LightGBM Booster o sklearn estimators
    try:
        proba = _model.predict_proba(X)[0, 1]  # sklearn-style
    except AttributeError:
        proba = _model.predict(X)[0]           # LightGBM Booster
    return float(proba)


def reload_model() -> None:
    """Borra el modelo en memoria para forzar recarga (p. ej. tras retrain)."""
    global _model, _model_mtime, _meta_cache, _meta_mtime
    with _model_lock:
        _model = None
        _model_mtime = None
        _meta_cache = None
        _meta_mtime = None
    _load_model()
    log.info("🔄 Modelo recargado manualmente (forzando reload en memoria)")

def model_runtime_status() -> dict[str, Any]:
    """Estado ligero del modelo y de su activación recomendada."""
    meta = _load_meta()
    train_status = _load_train_status()
    _load_model()
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
    return {
        "model_exists": _MODEL_PATH.exists(),
        "meta_exists": _META_PATH.exists(),
        "model_loaded": _model is not None,
        "features_count": len(_FEATURES or ()),
        "activation_ready": meta.get("activation_ready"),
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
        "model_path": str(_MODEL_PATH),
        "meta_path": str(_META_PATH),
        "train_status_path": str(_TRAIN_STATUS_PATH),
    }


def threshold_runtime_metadata() -> dict[str, Any]:
    """Threshold metadata with by-lane support and legacy fallback."""
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


__all__ = ["should_buy", "reload_model", "model_runtime_status", "threshold_runtime_metadata"]
