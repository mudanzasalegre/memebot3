from __future__ import annotations

import json
import logging
import pathlib
import shutil
from typing import Optional, Tuple

from utils.venv_bootstrap import ensure_project_venv

ensure_project_venv(__file__, module_name=__spec__.name if __spec__ else None)

from config.config import CFG
from ml.train import RECOMMENDED_JSON, TRAIN_STATUS_JSON, TrainResult, train_and_save

log = logging.getLogger("ml.retrain")

MODEL_PATH: pathlib.Path = CFG.MODEL_PATH
META_PATH: pathlib.Path = MODEL_PATH.with_suffix(".meta.json")


def _load_meta(meta_path: pathlib.Path) -> dict:
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _selection(metric_meta: dict) -> tuple[str | None, float | None]:
    metric = metric_meta.get("model_selection_metric")
    score = metric_meta.get("model_selection_score")
    if isinstance(metric, str) and isinstance(score, (int, float)):
        return metric, float(score)
    auc_pr = metric_meta.get("auc_pr_forward_or_cv_mean")
    if isinstance(auc_pr, (int, float)):
        return "auc_pr_forward_or_cv_mean", float(auc_pr)
    return None, None


def _selection_min_delta(metric: str | None) -> float:
    if metric == "avg_realized_pnl_pct_at_picked":
        return float(getattr(CFG, "ML_SELECTION_MIN_DELTA", 0.25))
    return 0.005


def _backup_old(
    model: pathlib.Path,
    meta: pathlib.Path,
    threshold_json: pathlib.Path,
) -> Tuple[Optional[pathlib.Path], Optional[pathlib.Path], Optional[pathlib.Path]]:
    b_model = b_meta = b_thr = None
    if model.exists():
        b_model = model.parent / f"{model.stem}.bkup.pkl"
        shutil.copy2(model, b_model)
    if meta.exists():
        b_meta = meta.parent / f"{meta.stem}.bkup.json"
        shutil.copy2(meta, b_meta)
    if threshold_json.exists():
        b_thr = threshold_json.parent / f"{threshold_json.stem}.bkup.json"
        shutil.copy2(threshold_json, b_thr)
    return b_model, b_meta, b_thr


def _restore_backup(
    model: pathlib.Path,
    meta: pathlib.Path,
    threshold_json: pathlib.Path,
    b_model: Optional[pathlib.Path],
    b_meta: Optional[pathlib.Path],
    b_thr: Optional[pathlib.Path],
) -> None:
    if b_model is not None and b_model.exists():
        shutil.move(str(b_model), str(model))
    if b_meta is not None and b_meta.exists():
        shutil.move(str(b_meta), str(meta))
    if b_thr is not None and b_thr.exists():
        shutil.move(str(b_thr), str(threshold_json))


def _cleanup_backups(*paths: Optional[pathlib.Path]) -> None:
    for path in paths:
        try:
            if path is not None and path.exists():
                path.unlink(missing_ok=True)
        except Exception:
            pass


def retrain_if_better() -> bool:
    prev_meta = _load_meta(META_PATH)
    prev_metric, prev_score = _selection(prev_meta)

    b_model, b_meta, b_thr = _backup_old(MODEL_PATH, META_PATH, RECOMMENDED_JSON)
    try:
        result: TrainResult = train_and_save()
    except Exception:
        _restore_backup(MODEL_PATH, META_PATH, RECOMMENDED_JSON, b_model, b_meta, b_thr)
        _cleanup_backups(b_model, b_meta, b_thr)
        raise

    if not result.trained:
        _cleanup_backups(b_model, b_meta, b_thr)
        log.info("⚪ Retrain omitido: %s (ver %s)", result.status, TRAIN_STATUS_JSON)
        return False

    new_meta = _load_meta(META_PATH)
    new_metric, new_score = _selection(new_meta)

    if prev_score is None or prev_metric is None:
        _cleanup_backups(b_model, b_meta, b_thr)
        log.info(
            "✅ Modelo entrenado por primera vez (%s=%s, activation_ready=%s)",
            new_metric,
            new_score,
            new_meta.get("activation_ready"),
        )
        return True

    if new_score is None or new_metric is None:
        log.info("❌ El nuevo entrenamiento no dejó métrica de selección válida; se conserva el modelo previo")
        _restore_backup(MODEL_PATH, META_PATH, RECOMMENDED_JSON, b_model, b_meta, b_thr)
        _cleanup_backups(b_model, b_meta, b_thr)
        return False

    metric_for_compare = new_metric if new_metric == prev_metric else "auc_pr_forward_or_cv_mean"
    if metric_for_compare == "auc_pr_forward_or_cv_mean":
        prev_score = float(prev_meta.get("auc_pr_forward_or_cv_mean") or prev_meta.get("auc_pr_mean") or 0.0)
        new_score = float(new_meta.get("auc_pr_forward_or_cv_mean") or 0.0)

    min_delta = _selection_min_delta(metric_for_compare)
    improvement = float(new_score) - float(prev_score)
    if improvement >= float(min_delta):
        _cleanup_backups(b_model, b_meta, b_thr)
        log.info(
            "✅ Modelo actualizado %s %.4f → %.4f (Δ=+%.4f, activation_ready=%s)",
            metric_for_compare,
            prev_score,
            new_score,
            improvement,
            new_meta.get("activation_ready"),
        )
        return True

    log.info(
        "❌ Sin mejora suficiente (%s Δ=%.4f < %.4f). Se mantiene el modelo previo.",
        metric_for_compare,
        improvement,
        min_delta,
    )
    _restore_backup(MODEL_PATH, META_PATH, RECOMMENDED_JSON, b_model, b_meta, b_thr)
    _cleanup_backups(b_model, b_meta, b_thr)
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    retrain_if_better()
