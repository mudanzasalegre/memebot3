# ml/retrain.py
"""
ml.retrain
~~~~~~~~~~
Re-entrena y sustituye el modelo sólo si la métrica AUC-PR mejora
al menos `min_delta` (por defecto 0.005 = +0.5 pp).

NOVEDADES
---------
• Tras conservar el nuevo modelo (incluida primera vez), ejecuta el
  sintonizado de umbral (ml.tune_threshold) y deja siempre disponible
  data/metrics/recommended_threshold.json (placeholder si hace falta).
• Actualiza model.meta.json con:
    - ai_threshold_recommended
    - tune_objective
    - tune_metrics (precision/recall/F1, AUC-PR, ROC-AUC, etc.)
• Rollback seguro de modelo/meta/threshold si no hay mejora.

Uso
---
python -m ml.retrain
"""
from __future__ import annotations

import json
import logging
import pathlib
import shutil
from typing import Tuple, Optional

from config.config import CFG
from ml.train import train_and_save  # entrena y guarda modelo + meta

log = logging.getLogger("ml.retrain")

# ───────────────────────── paths ────────────────────────────────
MODEL_PATH: pathlib.Path = CFG.MODEL_PATH
META_PATH: pathlib.Path = MODEL_PATH.with_suffix(".meta.json")

# Rutas del tuner/metrics
try:
    from ml.tune_threshold import main as _tune_main  # CLI-friendly
    from ml.tune_threshold import OUT_JSON as TUNE_JSON_PATH
except Exception:  # pragma: no cover
    _tune_main = None  # type: ignore
    TUNE_JSON_PATH = (CFG.FEATURES_DIR.parent / "metrics" / "recommended_threshold.json")


# ───────────────────────── helpers ──────────────────────────────
def _load_auc_pr(meta_path: pathlib.Path) -> Optional[float]:
    """Carga AUC-PR (o métrica aproximada) desde el .meta.json."""
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:  # pragma: no cover
        return None
    for k in ("auc_pr_mean", "auc_cv_mean", "auc"):
        v = meta.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _backup_old(model: pathlib.Path, meta: pathlib.Path, tune_json: pathlib.Path) -> Tuple[Optional[pathlib.Path], Optional[pathlib.Path], Optional[pathlib.Path]]:
    """
    Copia modelo/meta/threshold actuales a .bkup.* y devuelve sus rutas.
    Si alguno no existe, devuelve None en su lugar.
    """
    tmp_model = tmp_meta = tmp_tune = None

    if model.exists():
        tmp_model = model.parent / (model.stem + ".bkup.pkl")
        shutil.copy2(model, tmp_model)

    if meta.exists():
        tmp_meta = meta.parent / (meta.stem + ".bkup.json")
        shutil.copy2(meta, tmp_meta)

    if tune_json.exists():
        tmp_tune = tune_json.parent / (tune_json.stem + ".bkup.json")
        shutil.copy2(tune_json, tmp_tune)

    return tmp_model, tmp_meta, tmp_tune


def _restore_backup(
    model: pathlib.Path, meta: pathlib.Path, tune_json: pathlib.Path,
    b_model: Optional[pathlib.Path], b_meta: Optional[pathlib.Path], b_tune: Optional[pathlib.Path],
) -> None:
    """Restaura backups (si existen) sobre los ficheros reales."""
    if b_model is not None:
        shutil.move(str(b_model), str(model))
    if b_meta is not None:
        shutil.move(str(b_meta), str(meta))
    if b_tune is not None:
        shutil.move(str(b_tune), str(tune_json))


def _write_placeholder_threshold(path: pathlib.Path) -> None:
    """Escribe un JSON de threshold placeholder (=0.5) para no dejar el sistema sin archivo."""
    data = {
        "picked": 0.5,
        "objective": "degenerate",
        "f1_at_picked": 0.0,
        "precision_at_picked": 0.0,
        "recall_at_picked": 0.0,
        "auc_pr": float("nan"),
        "roc_auc": float("nan"),
        "samples": 0,
        "positives": 0,
        "source_csv": str(CFG.FEATURES_DIR.parent / "metrics" / "val_preds.csv"),
        "generated_at_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alternatives": {},
        "note": "Placeholder: generar real con ml.tune_threshold en cuanto haya val_preds.csv.",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _augment_meta_with_threshold(meta_path: pathlib.Path, tune_json_path: pathlib.Path) -> None:
    """
    Inserta en el .meta.json la información del umbral recomendado si existe
    el JSON producido por ml.tune_threshold (o placeholder).
    """
    if not meta_path.exists() or not tune_json_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text())
        tune = json.loads(tune_json_path.read_text())
    except Exception as exc:  # pragma: no cover
        log.debug("No se pudo leer meta/tune JSON: %s", exc)
        return

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

    tmp = meta_path.with_suffix(".meta.json.tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(meta_path)


def _run_tuner() -> float | None:
    """
    Ejecuta el sintonizador de umbral (ml.tune_threshold) y devuelve
    el valor recomendado si todo fue bien. Si no genera JSON, crea un
    placeholder para no dejar al bot sin archivo.
    """
    if _tune_main is None:
        log.warning("ml.tune_threshold no disponible; se escribirá placeholder.")
        _write_placeholder_threshold(TUNE_JSON_PATH)
        return 0.5

    try:
        # Ejecuta con parámetros por defecto (objective=f1).
        _tune_main()
    except SystemExit:
        # argparse puede lanzar SystemExit(0); lo consideramos OK.
        pass
    except Exception as exc:  # pragma: no cover
        log.warning("Fallo ejecutando ml.tune_threshold: %s", exc)

    if not TUNE_JSON_PATH.exists():
        log.warning("Tuner no generó %s; escribo placeholder.", TUNE_JSON_PATH)
        _write_placeholder_threshold(TUNE_JSON_PATH)
        return 0.5

    try:
        data = json.loads(TUNE_JSON_PATH.read_text())
        picked = data.get("picked")
        return float(picked) if isinstance(picked, (int, float)) else None
    except Exception as exc:  # pragma: no cover
        log.warning("No se pudo leer %s: %s. Escribo placeholder.", TUNE_JSON_PATH, exc)
        _write_placeholder_threshold(TUNE_JSON_PATH)
        return 0.5


# ───────────────────── función principal ──────────────────────
def retrain_if_better(min_delta: float = 0.005) -> bool:
    """
    • Lanza `train_and_save()` – genera modelo + meta + val_preds.csv.
    • Compara el nuevo AUC-PR con el antiguo.
    • Si mejora ≥ `min_delta`  → mantiene el nuevo y ejecuta el tuner.
      Si NO                   → restaura el anterior (modelo/meta/threshold).

    Returns
    -------
    bool
        True  → modelo actualizado (y threshold sintonizado/placeholder)
        False → se conserva el modelo previo
    """
    prev_auc_pr = _load_auc_pr(META_PATH)
    b_model, b_meta, b_tune = _backup_old(MODEL_PATH, META_PATH, TUNE_JSON_PATH)

    # Entrena y guarda (modelo.pkl + modelo.meta.json + val_preds.csv)
    train_and_save()
    new_auc_pr = _load_auc_pr(META_PATH)

    # — primera vez —
    if prev_auc_pr is None:
        log.info("✅ Modelo entrenado por primera vez (AUC-PR=%.4f)", new_auc_pr or float("nan"))
        picked = _run_tuner()
        _augment_meta_with_threshold(META_PATH, TUNE_JSON_PATH)
        if picked is not None:
            log.info("🎯 Umbral recomendado (AI_THRESHOLD)=%.3f (ver %s)", picked, TUNE_JSON_PATH)
        return True

    # — comparar y decidir —
    improvement = (new_auc_pr or 0.0) - (prev_auc_pr or 0.0)
    if improvement >= min_delta:
        log.info(
            "✅ Modelo actualizado  AUC-PR %.4f → %.4f  (Δ=+%.4f)",
            prev_auc_pr, new_auc_pr, improvement,
        )
        picked = _run_tuner()
        _augment_meta_with_threshold(META_PATH, TUNE_JSON_PATH)
        if picked is not None:
            log.info("🎯 Umbral recomendado (AI_THRESHOLD)=%.3f (ver %s)", picked, TUNE_JSON_PATH)

        # limpia backups residuales
        for p in (b_model, b_meta, b_tune):
            try:
                if p is not None:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
        return True

    # — sin mejora → rollback completo —
    log.info("❌ Sin mejora (Δ=%.4f < %.4f) – se mantiene el modelo previo", improvement, min_delta)
    _restore_backup(MODEL_PATH, META_PATH, TUNE_JSON_PATH, b_model, b_meta, b_tune)
    # limpia backups residuales si quedaron
    for p in (b_model, b_meta, b_tune):
        try:
            if p is not None and p.exists():
                p.unlink(missing_ok=True)
        except Exception:
            pass
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    retrain_if_better()
