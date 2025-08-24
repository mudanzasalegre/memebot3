# ml/retrain.py
"""
ml.retrain
~~~~~~~~~~
Re-entrena y sustituye el modelo sólo si la métrica AUC-PR mejora
al menos `min_delta` (por defecto 0.005 = +0.5 pp).

NOVEDADES
---------
• Al conservarse el nuevo modelo (incluida primera vez), ejecuta el
  sintonizado de umbral (ml.tune_threshold) y añade al .meta.json:
    - ai_threshold_recommended
    - tune_objective
    - tune_metrics (precision/recall/F1 en el umbral elegido, AUCs, etc.)
• Logging robusto y rollback seguro de modelo/meta en caso de no mejora.

Uso
---
python -m ml.retrain
"""
from __future__ import annotations

import json
import logging
import pathlib
import shutil
from typing import Tuple

from config.config import CFG
from ml.train import train_and_save  # entrena y guarda modelo + meta

log = logging.getLogger("ml.retrain")

# ───────────────────────── paths ────────────────────────────────
MODEL_PATH: pathlib.Path = CFG.MODEL_PATH
META_PATH: pathlib.Path = MODEL_PATH.with_suffix(".meta.json")

# Rutas del tuner (comparten METRICS_DIR con train)
try:
    from ml.tune_threshold import main as _tune_main  # función CLI-friendly
    from ml.tune_threshold import OUT_JSON as TUNE_JSON_PATH
except Exception:  # pragma: no cover
    _tune_main = None  # type: ignore
    TUNE_JSON_PATH = (CFG.FEATURES_DIR.parent / "metrics" / "recommended_threshold.json")


# ───────────────────────── helpers ──────────────────────────────
def _load_auc_pr(meta_path: pathlib.Path) -> float | None:
    """Carga AUC-PR (o métrica aproximada) desde el .meta.json."""
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:  # pragma: no cover
        return None
    # Prioridad: auc_pr_mean → auc_cv_mean → auc
    for k in ("auc_pr_mean", "auc_cv_mean", "auc"):
        v = meta.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _backup_old_model() -> Tuple[pathlib.Path, pathlib.Path] | None:
    """
    Copia modelo+meta actuales a ficheros .bkup.* en el mismo directorio
    y devuelve sus rutas (tmp_model, tmp_meta).
    """
    if not MODEL_PATH.exists() or not META_PATH.exists():
        return None
    tmp_model = MODEL_PATH.parent / (MODEL_PATH.stem + ".bkup.pkl")
    tmp_meta = META_PATH.parent / (META_PATH.stem + ".bkup.json")
    shutil.copy2(MODEL_PATH, tmp_model)
    shutil.copy2(META_PATH, tmp_meta)
    return tmp_model, tmp_meta


def _restore_backup(paths: Tuple[pathlib.Path, pathlib.Path]) -> None:
    """Restaura el modelo/meta anteriores en caso de que el nuevo no mejore."""
    tmp_model, tmp_meta = paths
    try:
        shutil.move(tmp_model, MODEL_PATH)
    finally:
        # Si move falla, al menos intenta copiar de vuelta
        if not MODEL_PATH.exists() and tmp_model.exists():
            shutil.copy2(tmp_model, MODEL_PATH)
    try:
        shutil.move(tmp_meta, META_PATH)
    finally:
        if not META_PATH.exists() and tmp_meta.exists():
            shutil.copy2(tmp_meta, META_PATH)


def _augment_meta_with_threshold(meta_path: pathlib.Path, tune_json_path: pathlib.Path) -> None:
    """
    Inserta en el .meta.json la información del umbral recomendado si existe
    el JSON producido por ml.tune_threshold.
    """
    if not meta_path.exists() or not tune_json_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text())
        tune = json.loads(tune_json_path.read_text())
    except Exception as exc:  # pragma: no cover
        log.debug("No se pudo leer meta/tune JSON: %s", exc)
        return

    # Campos añadidos
    meta["ai_threshold_recommended"] = tune.get("picked")
    meta["tune_objective"] = tune.get("objective")
    meta["tune_metrics"] = {
        "f1": tune.get("f1_at_picked"),
        "precision": tune.get("precision_at_picked"),
        "recall": tune.get("recall_at_picked"),
        "auc_pr": tune.get("auc_pr"),
        "roc_auc": tune.get("roc_auc"),
        "samples": tune.get("samples"),
        "positives": tune.get("positives"),
    }

    # Escritura segura
    tmp = meta_path.with_suffix(".meta.json.tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(meta_path)


def _run_tuner() -> float | None:
    """
    Ejecuta el sintonizador de umbral (ml.tune_threshold) y devuelve
    el valor recomendado (float) si todo fue bien.
    """
    if _tune_main is None:
        log.warning("ml.tune_threshold no disponible; omito sintonizado de umbral.")
        return None

    try:
        # Ejecuta con parámetros por defecto (objective=f1).
        _tune_main()
    except SystemExit:
        # argparse puede hacer SystemExit(0). Lo consideramos OK.
        pass
    except Exception as exc:  # pragma: no cover
        log.warning("Fallo ejecutando ml.tune_threshold: %s", exc)
        return None

    # Lee el JSON resultante
    if not TUNE_JSON_PATH.exists():
        log.warning("Tuner no generó %s; omito ai_threshold_recommended.", TUNE_JSON_PATH)
        return None
    try:
        data = json.loads(TUNE_JSON_PATH.read_text())
        picked = data.get("picked")
        if isinstance(picked, (int, float)):
            return float(picked)
        return None
    except Exception as exc:  # pragma: no cover
        log.warning("No se pudo leer %s: %s", TUNE_JSON_PATH, exc)
        return None


# ───────────────────── función principal ──────────────────────
def retrain_if_better(min_delta: float = 0.005) -> bool:
    """
    • Lanza `train_and_save()` – genera modelo + meta.
    • Compara el nuevo AUC-PR con el antiguo.
    • Si mejora ≥ `min_delta`  → mantiene el nuevo y ejecuta tuner de umbral.
      Si NO                   → restaura el antiguo.

    Returns
    -------
    bool
        True  → modelo actualizado (y umbral sintonizado si fue posible)
        False → se conserva el modelo previo
    """
    prev_auc_pr = _load_auc_pr(META_PATH)
    backup = _backup_old_model()  # None si no existía modelo previo

    # Entrena y guarda (modelo.pkl + modelo.meta.json + val_preds.csv)
    train_and_save()
    new_auc_pr = _load_auc_pr(META_PATH)

    # — primera vez —
    if prev_auc_pr is None:
        log.info("✅ Modelo entrenado por primera vez (AUC-PR=%.4f)", new_auc_pr or float("nan"))
        # Sintoniza umbral al tener primer modelo
        picked = _run_tuner()
        if picked is not None:
            _augment_meta_with_threshold(META_PATH, TUNE_JSON_PATH)
            log.info("🎯 Umbral recomendado (AI_THRESHOLD)=%.3f (ver %s)", picked, TUNE_JSON_PATH)
        else:
            log.info("🎯 Umbral recomendado no disponible (ver logs del tuner).")
        return True

    # — comparar y decidir —
    improvement = (new_auc_pr or 0.0) - (prev_auc_pr or 0.0)
    if improvement >= min_delta:
        log.info(
            "✅ Modelo actualizado  AUC-PR %.4f → %.4f  (Δ=+%.4f)",
            prev_auc_pr, new_auc_pr, improvement,
        )
        # El nuevo se queda → ejecuta tuner y anexa al meta
        picked = _run_tuner()
        if picked is not None:
            _augment_meta_with_threshold(META_PATH, TUNE_JSON_PATH)
            log.info("🎯 Umbral recomendado (AI_THRESHOLD)=%.3f (ver %s)", picked, TUNE_JSON_PATH)
        else:
            log.info("🎯 Umbral recomendado no disponible (ver logs del tuner).")

        # limpia backups si los hay
        if backup:
            for p in backup:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
        return True

    # — sin mejora → rollback —
    log.info(
        "❌ Sin mejora (Δ=%.4f < %.4f) – se mantiene el modelo previo",
        improvement, min_delta,
    )
    if backup:
        _restore_backup(backup)
        # borra backups residuales
        for p in backup:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    return False


if __name__ == "__main__":
    # Permite ejecutar `python -m ml.retrain` manualmente
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    retrain_if_better()
