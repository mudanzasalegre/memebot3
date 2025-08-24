# ml/train.py
"""
Entrena un LightGBM binario con validación temporal estratificada y guarda:
  • modelo        → CFG.MODEL_PATH
  • metadatos     → CFG.MODEL_PATH.with_suffix(".meta.json")
  • predicciones  → data/metrics/val_preds.csv  (para tunear AI_THRESHOLD)

Notas:
- Lee Parquet o CSV en CFG.FEATURES_DIR (pattern: features_*.parquet/.csv).
- Split temporal por días, garantizando ≥1 positivo en cada test.
- Convierte booleanos a int y limpia constantes.
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
from sklearn.metrics import average_precision_score, roc_auc_score, roc_curve

from config.config import CFG

# ───────────────────────── paths ──────────────────────────────
DATA_DIR: pathlib.Path = CFG.FEATURES_DIR
MODEL_PATH: pathlib.Path = CFG.MODEL_PATH
META_PATH: pathlib.Path = MODEL_PATH.with_suffix(".meta.json")
METRICS_DIR: pathlib.Path = DATA_DIR.parent / "metrics"
VAL_PREDS_CSV: pathlib.Path = METRICS_DIR / "val_preds.csv"


# ───────────────────── helpers de carga ───────────────────────
def _coerce_timestamp(df: pd.DataFrame) -> pd.DataFrame:
    """Intenta garantizar una columna datetime 'timestamp'."""
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        return df
    # fallbacks comunes
    for cand in ("ts", "created_at", "listed_at"):
        if cand in df.columns:
            df["timestamp"] = pd.to_datetime(df[cand], utc=True, errors="coerce")
            return df
    # si no hay nada, crea un índice temporal sintético (evita crashear)
    df["timestamp"] = pd.to_datetime("now", utc=True)
    return df


def _load_one(path: str) -> pd.DataFrame:
    if path.endswith(".parquet"):
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path)
    return _coerce_timestamp(df)


def _load_dataset() -> pd.DataFrame:
    """Concatena todos los Parquet/CSV en FEATURES_DIR."""
    files = sorted(
        glob.glob(str(DATA_DIR / "features_*.parquet"))
        + glob.glob(str(DATA_DIR / "features_*.csv"))
    )
    if not files:
        raise FileNotFoundError(
            f"No se encontró features_*.parquet/csv en {DATA_DIR}"
        )

    df = pd.concat([_load_one(f) for f in files], ignore_index=True)

    # tipado coherente (bool → int)
    for col in ("cluster_bad", "mint_auth_renounced", "social_ok", "trend"):
        if col in df.columns:
            df[col] = df[col].astype("int8")

    # mantener filas válidas
    if "label" not in df.columns:
        raise ValueError("El dataset no contiene la columna 'label'")

    df = df.dropna(subset=["label"]).copy()
    # opcional: filtra casos imposibles si existen columnas estándar
    for col in ("liquidity_usd", "volume_24h_usd"):
        if col in df.columns:
            df = df[df[col].fillna(0) >= 0]

    # address/mint normalizado para export de predicciones
    if "mint" not in df.columns:
        df["mint"] = (
            df["address"]
            if "address" in df.columns
            else df.get("token_address", pd.Series(index=df.index, dtype="object"))
        )

    return df


def _drop_constant_cols(df: pd.DataFrame, excl: List[str]) -> Tuple[pd.DataFrame, List[str]]:
    """Elimina columnas con varianza 0 y devuelve nuevas X_cols."""
    # solo numéricas para LightGBM (evita objetos)
    num_cols = [c for c in df.columns if c not in excl and pd.api.types.is_numeric_dtype(df[c])]
    zero_cols = [c for c in num_cols if df[c].std(skipna=True) == 0]
    df2 = df.drop(columns=zero_cols, errors="ignore")
    x_cols = [c for c in num_cols if c not in zero_cols]
    return df2, x_cols


# ───────────────────── splits estratificados ──────────────────
def _stratified_time_splits(
    df: pd.DataFrame,
    n_splits: int = 5,
) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Devuelve índices (train_idx, test_idx) garantizando ≥1 positivo por test."""
    df_sorted = df.sort_values("timestamp").reset_index(drop=True)
    # asegura tipo datetime
    df_sorted["timestamp"] = pd.to_datetime(df_sorted["timestamp"], utc=True, errors="coerce")
    dates = np.array(sorted(df_sorted["timestamp"].dt.date.unique()))
    folds = np.array_split(dates, n_splits)

    splits: List[Tuple[np.ndarray, np.ndarray]] = []
    for k, test_days in enumerate(folds):
        test_mask = df_sorted["timestamp"].dt.date.isin(test_days)
        # garantizar algún positivo
        y_test = df_sorted.loc[test_mask, "label"].values
        if y_test.sum() == 0 and k > 0 and len(folds[k - 1]) > 0:
            # roba 1 día del fold previo
            test_days = np.concatenate([folds[k - 1][-1:], test_days])
            folds[k - 1] = folds[k - 1][:-1]
            test_mask = df_sorted["timestamp"].dt.date.isin(test_days)

        train_idx = np.where(~test_mask)[0]
        test_idx = np.where(test_mask)[0]
        if len(test_idx) == 0:  # salvaguarda
            # mete el último día disponible para no dejar vacío
            test_idx = np.array([len(df_sorted) - 1])
            train_idx = np.arange(0, len(df_sorted) - 1)
        splits.append((train_idx, test_idx))
    return splits


# ───────────────────── función principal ──────────────────────
def train_and_save() -> float:
    df_raw = _load_dataset()

    excl = [
        "label",
        "pnl",
        "timestamp",
        "ts",
        "pnl_pct",
        "address",
        "token_address",
        "pair_address",
        "symbol",
        "name",
        "discovered_via",
        "created_at",
        "mint",  # no usar como feature
    ]
    df, X_cols = _drop_constant_cols(df_raw, excl)

    # ——— Validación CV temporal estratificada ———
    cv_splits = _stratified_time_splits(df, n_splits=5)
    aucs, aps, thresholds, best_iters = [], [], [], []
    val_rows: List[pd.DataFrame] = []

    params = dict(
        objective="binary",
        metric="auc",
        learning_rate=0.05,
        num_leaves=64,
        is_unbalance=True,      # manejo auto de clases desbalanceadas
        verbosity=-1,
        seed=42,
    )

    for fold, (tr_idx, te_idx) in enumerate(cv_splits, 1):
        tr_df, te_df = df.iloc[tr_idx], df.iloc[te_idx]

        lgb_train = lgb.Dataset(tr_df[X_cols], tr_df["label"])
        lgb_test = lgb.Dataset(te_df[X_cols], te_df["label"])

        model = lgb.train(
            params,
            lgb_train,
            num_boost_round=800,
            valid_sets=[lgb_test],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )

        y_pred = model.predict(te_df[X_cols], num_iteration=model.best_iteration)
        auc = roc_auc_score(te_df["label"], y_pred)
        ap = average_precision_score(te_df["label"], y_pred)
        aucs.append(auc)
        aps.append(ap)
        best_iters.append(model.best_iteration or 100)

        fpr, tpr, thr = roc_curve(te_df["label"], y_pred)
        j_stat = tpr - fpr
        best_thr = float(thr[np.argmax(j_stat)])
        thresholds.append(best_thr)

        # recolecta filas de validación con probas
        cols_extra = [c for c in ["liquidity_usd", "volume_24h_usd", "market_cap_usd", "holders"] if c in te_df.columns]
        fold_df = pd.DataFrame({
            "mint": df_raw.loc[te_idx, "mint"].values,
            "y_true": te_df["label"].values.astype(int),
            "y_prob": y_pred,
            "timestamp": df_raw.loc[te_idx, "timestamp"].values,
        })
        fold_df["hour"] = pd.to_datetime(fold_df["timestamp"], utc=True, errors="coerce").dt.hour
        for c in cols_extra:
            fold_df[c] = te_df[c].values
        fold_df["fold"] = fold
        val_rows.append(fold_df)

        print(f"[CV] Fold{fold}: AUC={auc:.4f}  AP={ap:.4f}  thr*={best_thr:.3f}  it*={model.best_iteration}")

    print(f"[CV] Mean AUC={np.mean(aucs):.4f} ± {np.std(aucs):.4f}")
    print(f"[CV] Mean AP ={np.mean(aps):.4f} ± {np.std(aps):.4f}")

    # —— exportar predicciones de validación ——
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    val_preds = pd.concat(val_rows, ignore_index=True)
    # orden de columnas agradable
    ordered = ["mint", "y_true", "y_prob", "timestamp", "hour"] + \
              [c for c in ["liquidity_usd", "volume_24h_usd", "market_cap_usd", "holders"] if c in val_preds.columns] + \
              ["fold"]
    val_preds[ordered].to_csv(VAL_PREDS_CSV, index=False)
    print(f"[ML] Predicciones de validación → {VAL_PREDS_CSV}")

    # —— entrenamiento final con todo el dataset —— 
    lgb_full = lgb.Dataset(df[X_cols], df["label"])
    final_params = params | {"metric": "auc"}
    final_num_boost_round = int(np.median(best_iters) if best_iters else 400)

    final_model = lgb.train(
        final_params,
        lgb_full,
        num_boost_round=final_num_boost_round,
    )

    # —— persistencia atómica del modelo ————————————————
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
        "threshold":   float(np.median(thresholds)),   # inicial; puede refinarse con val_preds
        "splits":      int(len(cv_splits)),
        "rows":        int(len(df)),
        "features":    X_cols,
        "final_num_boost_round": final_num_boost_round,
    }, indent=2))
    print(f"[ML] Modelo + meta guardados en {MODEL_PATH}")

    return float(np.mean(aucs))


if __name__ == "__main__":
    train_and_save()
