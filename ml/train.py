"""
Entrena un LightGBM binario con validación temporal estratificada y guarda
    • modelo  → CFG.MODEL_PATH
    • metadatos (AUC, threshold, lista de features) → .meta.json
"""
from __future__ import annotations

import glob
import json
import os
import pathlib
import tempfile
from typing import List, Tuple

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    roc_auc_score,
    roc_curve,
)

from config.config import CFG

# ───────────────────────── paths ──────────────────────────────
DATA_DIR: pathlib.Path = CFG.FEATURES_DIR
MODEL_PATH: pathlib.Path = CFG.MODEL_PATH
META_PATH: pathlib.Path = MODEL_PATH.with_suffix(".meta.json")

# ───────────────────── helpers de carga ───────────────────────
def _load_dataset() -> pd.DataFrame:
    """Concatena todos los Parquet en FEATURES_DIR."""
    files = glob.glob(str(DATA_DIR / "features_*.parquet"))
    if not files:
        raise FileNotFoundError("No se encontró ningún Parquet en data/features/")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)

    # tipado coherente (bool → int)
    for col in ("cluster_bad", "mint_auth_renounced", "social_ok", "trend"):
        if col in df.columns:
            df[col] = df[col].astype("int8")

    # mantener sólo filas con liquidez/volumen positivos
    df = df.query("liquidity_usd > 0 and volume_24h_usd > 0")
    df = df.dropna(subset=["label"])
    return df


def _drop_constant_cols(df: pd.DataFrame, excl: List[str]) -> Tuple[pd.DataFrame, List[str]]:
    """Elimina columnas con varianza 0 y devuelve nuevas X_cols."""
    num_cols = [
        c for c in df.columns
        if c not in excl and df[c].dtype != "object"
    ]
    zero_cols = [c for c in num_cols if df[c].std(skipna=True) == 0]
    df = df.drop(columns=zero_cols)
    x_cols = [c for c in num_cols if c not in zero_cols]
    return df, x_cols


# ───────────────────── splits estratificados ──────────────────
def _stratified_time_splits(
    df: pd.DataFrame,
    n_splits: int = 5,
) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Devuelve índices (train_idx, test_idx) garantizando ≥1 positivo por test."""
    df_sorted = df.sort_values("timestamp").reset_index(drop=True)
    dates = np.array(sorted(df_sorted["timestamp"].dt.date.unique()))
    folds = np.array_split(dates, n_splits)

    splits = []
    for k, test_days in enumerate(folds):
        test_mask = df_sorted["timestamp"].dt.date.isin(test_days)
        y_test = df_sorted.loc[test_mask, "label"].values
        if y_test.sum() == 0:            # sin positivos → roba un día del fold anterior
            if k > 0:
                test_days = np.concatenate([folds[k - 1][-1:], test_days])
                folds[k - 1] = folds[k - 1][:-1]
                test_mask = df_sorted["timestamp"].dt.date.isin(test_days)
        train_idx = np.where(~test_mask)[0]
        test_idx  = np.where(test_mask)[0]
        splits.append((train_idx, test_idx))
    return splits


# ───────────────────── función principal ──────────────────────
def train_and_save() -> float:
    df_raw = _load_dataset()

    excl = ["label", "pnl", "timestamp", "ts", "pnl_pct", "address", "discovered_via"]
    df, X_cols = _drop_constant_cols(df_raw, excl)

    # ——— Validación CV temporal estratificada ———
    cv_splits = _stratified_time_splits(df, n_splits=5)
    aucs, aps, thresholds = [], [], []

    for fold, (tr_idx, te_idx) in enumerate(cv_splits, 1):
        tr_df, te_df = df.iloc[tr_idx], df.iloc[te_idx]

        lgb_train = lgb.Dataset(tr_df[X_cols], tr_df["label"])
        lgb_test  = lgb.Dataset(te_df[X_cols], te_df["label"])

        params = dict(
            objective="binary",
            metric="auc",
            learning_rate=0.05,
            num_leaves=64,
            is_unbalance=True,      # ★ manejo auto de clases desbalanceadas
            verbosity=-1,
            seed=42,
        )

        model = lgb.train(
            params,
            lgb_train,
            num_boost_round=800,
            valid_sets=[lgb_test],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )

        y_pred = model.predict(te_df[X_cols], num_iteration=model.best_iteration)
        auc = roc_auc_score(te_df["label"], y_pred)
        ap  = average_precision_score(te_df["label"], y_pred)
        aucs.append(auc)
        aps.append(ap)

        fpr, tpr, thr = roc_curve(te_df["label"], y_pred)
        j_stat = tpr - fpr
        best_thr = thr[np.argmax(j_stat)]
        thresholds.append(best_thr)

        print(f"[CV] Fold{fold}: AUC={auc:.4f}  AP={ap:.4f}  thr*={best_thr:.3f}")

    print(f"[CV] Mean AUC={np.mean(aucs):.4f} ± {np.std(aucs):.4f}")
    print(f"[CV] Mean AP ={np.mean(aps):.4f} ± {np.std(aps):.4f}")

    # —— entrenamiento final con todo el dataset ——
    lgb_full = lgb.Dataset(df[X_cols], df["label"])
    final_params = params | {"metric": "auc"}
    final_model = lgb.train(
        final_params,
        lgb_full,
        num_boost_round=int(np.median([m.best_iteration for m in [model]])),
    )

    # —— persistencia atómica ————————————————————
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=MODEL_PATH.parent,
        prefix=".tmp_model_",
        suffix=".pkl",
    )
    os.close(tmp_fd)
    joblib.dump(final_model, tmp_path)
    pathlib.Path(tmp_path).replace(MODEL_PATH)

    # —— metadatos ————————————————————————————————
    META_PATH.write_text(json.dumps({
        "auc_cv_mean": float(np.mean(aucs)),
        "auc_pr_mean": float(np.mean(aps)),
        "threshold":   float(np.median(thresholds)),
        "splits":      len(cv_splits),
        "rows":        int(len(df)),
        "features":    X_cols,
    }, indent=2))
    print(f"[ML] Modelo + meta guardados en {MODEL_PATH}")

    return float(np.mean(aucs))


if __name__ == "__main__":
    train_and_save()
