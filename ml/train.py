# ml.train
# ~~~~~~~~
# Entrena el clasificador LightGBM a partir de los Parquet generados por
# features.store y guarda el modelo en `CFG.MODEL_PATH`.
from __future__ import annotations

import glob
import json
import os
import pathlib
import tempfile
from typing import Tuple

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

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

    # Filtra parches de PnL si los hubiera
    if "is_pnl_patch" in df.columns:
        df = df[df.get("is_pnl_patch", 0) != 1]

    # Tipado coherente
    for col in ("cluster_bad", "mint_auth_renounced", "social_ok", "trend"):
        if col in df.columns:
            df[col] = df[col].astype("int8")

    return df


def _split(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Split temporal: último 20 % como hold-out."""
    df = df.sort_values("timestamp")
    split_idx = int(len(df) * 0.8)
    return df.iloc[:split_idx], df.iloc[split_idx:]

# ───────────────────── función principal ──────────────────────
def train_and_save() -> float:
    df = _load_dataset().dropna(subset=["label"])

    # ── eliminar columnas de tipo object (texto) ──────────────
    obj_cols = df.select_dtypes(include="object").columns  # p.ej. ['address', 'discovered_via']

    X_cols = [
        c for c in df.columns
        if c not in ("label", "pnl", "timestamp", "ts", "pnl_pct")
        and c not in obj_cols
    ]

    train_df, valid_df = _split(df)

    pos, neg = np.bincount(train_df["label"].astype(int))
    scale_pos_weight = neg / max(pos, 1)

    lgb_train = lgb.Dataset(train_df[X_cols], train_df["label"])
    lgb_valid = lgb.Dataset(valid_df[X_cols], valid_df["label"])

    params = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.05,
        "num_leaves": 64,
        "scale_pos_weight": scale_pos_weight,
        "verbosity": -1,
    }

    model = lgb.train(
        params,
        lgb_train,
        valid_sets=[lgb_valid],
        num_boost_round=500,
        callbacks=[
            lgb.early_stopping(stopping_rounds=50, verbose=False),
            lgb.log_evaluation(period=0),  # silencia el output
        ],
    )

    auc = roc_auc_score(valid_df["label"], model.predict(valid_df[X_cols]))
    print(f"[ML] AUC hold-out = {auc:.4f}")

    # ───────── persistir de forma atómica (mismo volumen) ────────
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Creamos el temporal en la MISMA carpeta que MODEL_PATH
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=MODEL_PATH.parent,
        prefix=".tmp_model_",
        suffix=".pkl",
    )
    os.close(tmp_fd)            # cerramos para que Windows no bloquee
    joblib.dump(model, tmp_path)

    # rename/replace atómico dentro del mismo directorio/volumen
    pathlib.Path(tmp_path).replace(MODEL_PATH)

    # Metadatos
    META_PATH.write_text(json.dumps({
        "auc": auc,
        "train_rows": int(len(train_df)),
        "valid_rows": int(len(valid_df)),
        "features": X_cols,
    }, indent=2))
    print(f"[ML] Modelo guardado en {MODEL_PATH}")

    return auc


if __name__ == "__main__":
    train_and_save()
