"""
ml.retrain
~~~~~~~~~~
Re-entrena y sustituye el modelo sólo si la métrica AUC-PR mejora
al menos `min_delta` (por defecto 0.005 = +0.5 pp).
"""
from __future__ import annotations

import json
import pathlib
import shutil
import tempfile

from config.config import CFG
from ml.train import train_and_save  # noqa: WPS433  – import interno del paquete

MODEL_PATH  = CFG.MODEL_PATH
META_PATH   = MODEL_PATH.with_suffix(".meta.json")


# ───────────────────────── helpers ─────────────────────────────
def _load_auc_pr(meta_path: pathlib.Path) -> float | None:
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text())
    # Prioridad: auc_pr_mean  → auc_cv_mean → auc
    return meta.get("auc_pr_mean") or meta.get("auc_cv_mean") or meta.get("auc")


def _backup_old_model() -> tuple[pathlib.Path, pathlib.Path] | None:
    """
    Copia modelo+meta actuales a archivos temporales dentro del MISMO
    directorio y devuelve sus rutas (tmp_model, tmp_meta).
    """
    if not MODEL_PATH.exists():
        return None
    tmp_model = MODEL_PATH.parent / (MODEL_PATH.stem + ".bkup.pkl")
    tmp_meta  = META_PATH.parent  / (META_PATH.stem  + ".bkup.json")
    shutil.copy2(MODEL_PATH, tmp_model)
    shutil.copy2(META_PATH, tmp_meta)
    return tmp_model, tmp_meta


def _restore_backup(paths: tuple[pathlib.Path, pathlib.Path]) -> None:
    tmp_model, tmp_meta = paths
    shutil.move(tmp_model, MODEL_PATH)
    shutil.move(tmp_meta,  META_PATH)


# ───────────────────── función principal ──────────────────────
def retrain_if_better(min_delta: float = 0.005) -> bool:
    """
    • Lanza `train_and_save()` – (re)genera modelo + meta.
    • Compara el nuevo AUC-PR con el antiguo.
    • Si mejora ≥ `min_delta` → mantiene el nuevo.
      Si NO → restaura el antiguo y descarta entrenamiento.
    Returns
    -------
    bool
        True  → modelo actualizado  
        False → se conserva el modelo previo
    """
    prev_auc_pr = _load_auc_pr(META_PATH)
    backup = _backup_old_model()        # None si no había modelo

    new_auc = train_and_save()          # ya guarda el modelo

    new_auc_pr = _load_auc_pr(META_PATH)

    if prev_auc_pr is None:
        print(f"[ML] ✅ Modelo entrenado por primera vez (AUC-PR={new_auc_pr:.4f})")
        return True

    improvement = (new_auc_pr or 0) - prev_auc_pr
    if improvement >= min_delta:
        print(f"[ML] ✅ Modelo actualizado  AUC-PR {prev_auc_pr:.4f} → {new_auc_pr:.4f}")
        # limpia backups provisionales
        if backup:
            backup[0].unlink(missing_ok=True)
            backup[1].unlink(missing_ok=True)
        return True

    # —— sin mejora → restaurar ——————————————————
    if backup:
        _restore_backup(backup)
    print(f"[ML] ❌ Sin mejora (Δ={improvement:.4f}) – se mantiene modelo previo")
    return False


if __name__ == "__main__":
    retrain_if_better()
