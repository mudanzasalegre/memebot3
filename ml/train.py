# ml/train.py
"""
Entrena un modelo binario (LightGBM) con validación *temporal estricta* (hold-out forward)
y guarda:
  • modelo        → CFG.MODEL_PATH
  • metadatos     → CFG.MODEL_PATH.with_suffix(".meta.json")
  • predicciones  → data/metrics/val_preds.csv  (para tunear AI_THRESHOLD)
  • umbral JSON   → data/metrics/recommended_threshold.json

Mejoras clave implementadas (v. hold-out forward):
──────────────────────────────────────────────────
- Split temporal ESTRICTO configurable:
    · por días desde el final (CFG.TRAIN_FORWARD_HOLDOUT_DAYS),
    · o por porcentaje del tramo final (CFG.TRAIN_FORWARD_HOLDOUT_PCT ∈ (0,1]).
  Si no se define ninguno, se usa el CV temporal agrupado por mint (back-compat).

- Separación por token (mint) garantizada: ningún mint aparece en train y val a la vez.

- Ventana de entrenamiento opcional: CFG.TRAINING_WINDOW_DAYS (filtra histórico a últimas N días).

- Exclusión robusta de columnas no permitidas (label/meta/resultado; también por patrón de nombre).

- Logging detallado: columnas excluidas efectivas, features finales y hash del feature-set.

- Importancias de variables (top-N) tras el fit (auditoría).

- Métricas de validación: AUC-PR (AP), F1, Precision@K (CFG.PRECISION_AT_K_PCT; por defecto 0.10).

- Umbral recomendado por F1 (sobre el hold-out forward) → JSON.
  Con suavizado: aplica solo si |nuevo-anterior| ≥ CFG.MIN_THRESHOLD_CHANGE (por defecto 0.0).

- (Opcional) Estimación simple de P&L simulado por umbral si se dispone de val_preds y
  CFG.SIMULATED_PNL_PER_WIN / CFG.SIMULATED_PNL_PER_FAIL (constantes de utilidad).

Requisitos mínimos de columnas:
  label (0/1), timestamp/ts/created_at/listed_at (al menos una), mint o address.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import pathlib
import tempfile
from typing import Dict, List, Tuple, Optional

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    precision_recall_curve,
    roc_auc_score,
    roc_curve,
    f1_score,
    precision_score,
    recall_score,
)

from config.config import CFG


# ───────────────────────── paths ──────────────────────────────
DATA_DIR: pathlib.Path = CFG.FEATURES_DIR
MODEL_PATH: pathlib.Path = CFG.MODEL_PATH
META_PATH: pathlib.Path = MODEL_PATH.with_suffix(".meta.json")
METRICS_DIR: pathlib.Path = DATA_DIR.parent / "metrics"
VAL_PREDS_CSV: pathlib.Path = METRICS_DIR / "val_preds.csv"
RECOMMENDED_JSON: pathlib.Path = METRICS_DIR / "recommended_threshold.json"

# ───────────────────── parámetros (con defaults) ──────────────
# Hold-out forward:
HOLDOUT_DAYS: Optional[int] = getattr(CFG, "TRAIN_FORWARD_HOLDOUT_DAYS", None)
HOLDOUT_PCT: Optional[float] = getattr(CFG, "TRAIN_FORWARD_HOLDOUT_PCT", None)  # ej. 0.2 → 20% final como val
# Ventana de entrenamiento:
TRAIN_WINDOW_DAYS: Optional[int] = getattr(CFG, "TRAINING_WINDOW_DAYS", None)
# Precision@K:
PREC_AT_K_PCT: float = float(getattr(CFG, "PRECISION_AT_K_PCT", 0.10))  # 10% por defecto
# Suavizado de umbral:
MIN_THR_CHANGE: float = float(getattr(CFG, "MIN_THRESHOLD_CHANGE", 0.0))
# P&L simulado (opcional, valores medios por trade para una estimación grosera):
SIM_PNL_PER_WIN: float = float(getattr(CFG, "SIMULATED_PNL_PER_WIN", 1.0))
SIM_PNL_PER_FAIL: float = float(getattr(CFG, "SIMULATED_PNL_PER_FAIL", -1.0))

# ───────────────────── helpers de carga ───────────────────────
def _coerce_timestamp(df: pd.DataFrame) -> pd.DataFrame:
    """Garantiza una columna datetime 'timestamp' (UTC) a partir de varias alternativas."""
    for cand in ("timestamp", "ts", "created_at", "listed_at"):
        if cand in df.columns:
            df["timestamp"] = pd.to_datetime(df[cand], utc=True, errors="coerce")
            break
    else:
        # si no hay nada, crea un índice temporal sintético (evita crashear; peor calidad)
        df["timestamp"] = pd.to_datetime("now", utc=True)
    return df


def _load_one(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
    return _coerce_timestamp(df)


def _load_dataset() -> pd.DataFrame:
    """Concatena todos los Parquet/CSV en FEATURES_DIR."""
    files = sorted(
        glob.glob(str(DATA_DIR / "features_*.parquet"))
        + glob.glob(str(DATA_DIR / "features_*.csv"))
    )
    if not files:
        raise FileNotFoundError(f"No se encontró features_*.parquet/csv en {DATA_DIR}")

    df = pd.concat([_load_one(f) for f in files], ignore_index=True)

    # tipado coherente (bool → int)
    for col in ("cluster_bad", "mint_auth_renounced", "social_ok", "is_incomplete"):
        if col in df.columns:
            df[col] = df[col].astype("int8")

    # mantener filas válidas
    if "label" not in df.columns:
        raise ValueError("El dataset no contiene la columna 'label'")
    df = df.dropna(subset=["label"]).copy()

    # address/mint normalizado para export de predicciones y validación agrupada
    if "mint" not in df.columns:
        df["mint"] = (
            df["address"]
            if "address" in df.columns
            else df.get("token_address", pd.Series(index=df.index, dtype="object"))
        )

    # aseguramos string para mint
    df["mint"] = df["mint"].astype("string")

    return df


def _apply_training_window(df: pd.DataFrame) -> pd.DataFrame:
    """Si TRAIN_WINDOW_DAYS está definido, filtra a últimas N días según timestamp global."""
    if TRAIN_WINDOW_DAYS and "timestamp" in df.columns:
        tmax = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").max()
        cutoff = tmax - pd.Timedelta(days=int(TRAIN_WINDOW_DAYS))
        df = df[df["timestamp"] >= cutoff].copy()
        print(f"[WIN] Ventana de entrenamiento aplicada: últimos {TRAIN_WINDOW_DAYS} días (cutoff={cutoff})")
    return df


# ─────────── exclusión robusta + selección X (numéricas) ──────
_FORBIDDEN_SUBSTR = (
    "pnl",          # 'pnl', 'pnl_pct', etc.
    "close_price",  # cualquier precio de cierre
    "_at_close",
    "_after_",
    "outcome",
    "result",
)
_META_COLS = (
    "label",
    "timestamp", "ts", "created_at", "listed_at",
    "address", "token_address", "pair_address",
    "symbol", "name", "discovered_via",
    "mint",   # nunca usar como feature
)

def _drop_constant_and_non_numeric(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str], List[str]]:
    """
    Elimina columnas con varianza 0 y deja solo numéricas (LightGBM maneja NaN).
    Aplica exclusión robusta por listas y por *patrón de nombre*.
    Devuelve: df_filtrado, X_cols, excluded_effective
    """
    excluded_effective: List[str] = []
    keep_candidate = []

    for c in df.columns:
        # exclusión por listas meta
        if c in _META_COLS:
            excluded_effective.append(c)
            continue
        # exclusión por patrón de nombre
        lc = c.lower()
        if any(sub in lc for sub in _FORBIDDEN_SUBSTR):
            excluded_effective.append(c)
            continue
        # solo numéricas
        if pd.api.types.is_numeric_dtype(df[c]):
            keep_candidate.append(c)

    # quitar varianza cero
    zero_cols = [c for c in keep_candidate if float(df[c].std(skipna=True) or 0.0) == 0.0]
    x_cols = [c for c in keep_candidate if c not in zero_cols]
    excluded_effective.extend(zero_cols)

    df2 = df.drop(columns=[c for c in excluded_effective if c in df.columns], errors="ignore")
    return df2, x_cols, excluded_effective


# ──────── CV temporal AGRUPADA por mint (back-compat) ─────────
def _grouped_time_splits_by_mint(
    df: pd.DataFrame,
    n_splits: int = 5,
    min_pos_per_fold: int = 1,
) -> List[Tuple[np.ndarray, np.ndarray]]:
    """
    Crea folds temporales *por grupo mint*:
      1) Ordena los mints por su primera aparición (min timestamp).
      2) Parte la lista de mints en `n_splits` bloques contiguos.
      3) Para cada fold k, el *test* son TODOS los registros cuyos mint∈bloque_k.
         El *train* son el resto (no hay intersección de mints → sin fuga).

    Garantiza >= min_pos_per_fold mints positivos por fold si es posible.
    """
    if "mint" not in df.columns:
        raise ValueError("Falta columna 'mint' para CV agrupada.")
    if "timestamp" not in df.columns:
        raise ValueError("Falta columna 'timestamp' (datetime) para CV temporal.")

    # timestamp por mint (primera vez que aparece)
    first_ts = (
        df.groupby("mint", dropna=False)["timestamp"]
        .min()
        .sort_values(kind="mergesort")  # estable
    )
    mints_sorted = first_ts.index.to_numpy()

    # etiqueta a nivel mint: positivo si el mint tiene alguna fila label=1
    mint_pos = df.groupby("mint", dropna=False)["label"].max()

    # corta en bloques contiguos de mints
    mint_blocks: List[np.ndarray] = np.array_split(mints_sorted, n_splits)

    # repara bloques sin positivos moviendo 1 mint positivo de vecinos (best effort)
    def _block_has_pos(block: np.ndarray) -> bool:
        if len(block) == 0:
            return False
        return bool(mint_pos.loc[list(block)].sum() > 0)

    for k in range(len(mint_blocks)):
        if _block_has_pos(mint_blocks[k]):
            continue
        for j in (k + 1, k - 1):
            if 0 <= j < len(mint_blocks) and len(mint_blocks[j]) > 0:
                neigh = mint_blocks[j]
                pos_mask = mint_pos.loc[list(neigh)].to_numpy(dtype=bool)
                if pos_mask.any():
                    idx = int(np.where(pos_mask)[0][-1])
                    mint_to_move = neigh[idx]
                    mint_blocks[j] = np.delete(neigh, idx)
                    mint_blocks[k] = np.append(mint_blocks[k], mint_to_move)
                    break

    # construir índices fila para cada fold (sin intersección de mints)
    splits: List[Tuple[np.ndarray, np.ndarray]] = []
    for block in mint_blocks:
        test_mask = df["mint"].isin(block)
        te_idx = np.where(test_mask.values)[0]
        tr_idx = np.where(~test_mask.values)[0]
        if len(te_idx) == 0:  # salvaguarda
            te_idx = np.array([len(df) - 1])
            tr_idx = np.arange(0, len(df) - 1)
        splits.append((tr_idx, te_idx))
    return splits


# ──────────────── util: métricas / umbrales / eval ────────────
def _best_threshold_f1(y_true: np.ndarray, y_prob: np.ndarray) -> Dict[str, float]:
    """
    Calcula el umbral que maximiza F1 a partir de la curva Prec-Rec.
    Devuelve dict con threshold, precision, recall, f1 y average_precision (AP).
    """
    ap = average_precision_score(y_true, y_prob) if y_true.sum() > 0 else 0.0
    precision, recall, thresholds = precision_recall_curve(y_true, y_prob)
    f1 = np.where((precision + recall) > 0, 2 * precision * recall / (precision + recall), 0.0)
    if thresholds.size == 0:
        # caso degenerado: usar el último punto (recall bajo, precision alta)
        return {
            "threshold": 0.5,
            "precision": float(precision[-1]),
            "recall": float(recall[-1]),
            "f1": float(f1[-1]),
            "ap": float(ap),
        }
    idx = int(np.argmax(f1[1:]))  # ignora el primer punto (recall=1.0, threshold=-inf)
    return {
        "threshold": float(thresholds[idx]),
        "precision": float(precision[idx + 1]),
        "recall": float(recall[idx + 1]),
        "f1": float(f1[idx + 1]),
        "ap": float(ap),
    }


def _precision_at_k(y_true: np.ndarray, y_prob: np.ndarray, k_pct: float = 0.1) -> float:
    """
    Precision@K: precisión en el top-K% de muestras por probabilidad.
    """
    k_pct = float(k_pct)
    n = y_prob.shape[0]
    if n == 0:
        return float("nan")
    k = max(1, int(round(n * k_pct)))
    order = np.argsort(-y_prob)  # descendente por prob
    top_idx = order[:k]
    return float(np.mean(y_true[top_idx]))


def _calibration_bins(y_true: np.ndarray, y_prob: np.ndarray, bins: int = 10) -> pd.DataFrame:
    """
    Tabla simple de calibración: por bins de probabilidad, proporción real de positivos.
    (Útil para inspección offline; no se persiste por defecto.)
    """
    df = pd.DataFrame({"y": y_true, "p": y_prob})
    df["bin"] = pd.qcut(df["p"].rank(method="first"), q=bins, labels=False)
    out = df.groupby("bin").agg(
        p_mean=("p", "mean"),
        y_rate=("y", "mean"),
        count=("y", "size"),
    ).reset_index(drop=True)
    return out


def _simple_pnl_estimate(y_true: np.ndarray, y_prob: np.ndarray, thr: float) -> Dict[str, float]:
    """
    Estimación grosera de P&L con constantes medias por trade.
    Compra si p>=thr; P&L = wins*SIM_PNL_PER_WIN + fails*SIM_PNL_PER_FAIL
    """
    select = y_prob >= thr
    buys = int(select.sum())
    if buys == 0:
        return {"buys": 0, "wins": 0, "fails": 0, "pnl": 0.0}
    wins = int((y_true[select] == 1).sum())
    fails = int(buys - wins)
    pnl = wins * SIM_PNL_PER_WIN + fails * SIM_PNL_PER_FAIL
    return {"buys": buys, "wins": wins, "fails": fails, "pnl": float(pnl)}


# ───────────────────── función principal ──────────────────────
def train_and_save() -> float:
    # 1) Carga
    df_raw = _load_dataset()
    df_raw = _apply_training_window(df_raw)

    # 2) Selección de X y exclusiones robustas
    df, X_cols, excluded_effective = _drop_constant_and_non_numeric(df_raw)

    # Logging de exclusiones y features finales
    print(f"[X] Excluyendo columnas (efectivas, {len(excluded_effective)}): {sorted(excluded_effective)}")
    feat_hash = hashlib.md5(",".join(sorted(X_cols)).encode("utf-8")).hexdigest()[:10]
    print(f"[X] Features finales ({len(X_cols)}). Hash={feat_hash}")
    # Guardar el hash también en metadatos más abajo

    # 3) Preparar split temporal ESTRICTO (hold-out forward) si está configurado
    use_forward = bool(HOLDOUT_DAYS or HOLDOUT_PCT)
    val_rows: List[pd.DataFrame] = []

    # Parametrización del modelo
    params = dict(
        objective="binary",
        metric="auc",
        learning_rate=0.05,
        num_leaves=64,
        is_unbalance=True,      # manejo auto de clases desbalanceadas
        verbosity=-1,
        seed=42,
        feature_fraction=0.9,
        bagging_fraction=0.9,
        bagging_freq=1,
    )

    # 3.a) HOLD-OUT FORWARD
    if use_forward:
        # Determinar cutoff temporal del hold-out
        t_series = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        tmin, tmax = t_series.min(), t_series.max()
        if HOLDOUT_DAYS:
            cutoff = tmax - pd.Timedelta(days=int(HOLDOUT_DAYS))
        else:
            # porcentaje final como validación
            # ordenamos por timestamp y cortamos por índice
            df_sorted = df.sort_values("timestamp")
            n = len(df_sorted)
            k = max(1, int(round(n * float(HOLDOUT_PCT))))
            cutoff = df_sorted.iloc[-k]["timestamp"]
        print(f"[SPLIT] Hold-out forward ACTIVADO. Cutoff={cutoff}  (tmin={tmin}, tmax={tmax})")

        # Separación estricta por mint (todo el mint a un lado según su primer timestamp)
        first_ts = df.groupby("mint", dropna=False)["timestamp"].min()
        val_mints = first_ts[first_ts >= cutoff].index
        train_mints = first_ts[first_ts < cutoff].index

        tr_mask = df["mint"].isin(train_mints)
        te_mask = df["mint"].isin(val_mints)

        tr_df, te_df = df[tr_mask].copy(), df[te_mask].copy()
        if len(te_df) == 0 or len(tr_df) == 0:
            raise RuntimeError("[SPLIT] Hold-out resultó vacío para train o val. Ajusta HOLDOUT_DAYS/PCT.")

        print(f"[SPLIT] Train rows={len(tr_df)}  Val rows={len(te_df)}  (train mints={tr_df['mint'].nunique()}, val mints={te_df['mint'].nunique()})")

        # Entrenamiento en train, evaluación en hold-out (forward)
        lgb_train = lgb.Dataset(tr_df[X_cols], tr_df["label"].values)
        model = lgb.train(
            params,
            lgb_train,
            num_boost_round=1600,
            valid_sets=[lgb_train],  # evitamos leaks; early_stopping no aplica con solo train
        )

        # Predicciones en hold-out
        y_te = te_df["label"].values.astype(int)
        y_pred = model.predict(te_df[X_cols], num_iteration=model.best_iteration)

        # Métricas
        try:
            auc = roc_auc_score(y_te, y_pred)
        except Exception:
            auc = float("nan")
        ap = average_precision_score(y_te, y_pred) if y_te.sum() > 0 else 0.0
        th_info = _best_threshold_f1(y_te, y_pred)
        prec_at_k = _precision_at_k(y_te, y_pred, k_pct=PREC_AT_K_PCT)

        print("[VAL] Forward hold-out:")
        print("      AUC={:.4f}  AP={:.4f}  F1@thr={:.4f}  P={:.4f}  R={:.4f}  Prec@{:.0f}%={:.4f}".format(
            auc, ap, th_info["f1"], th_info["precision"], th_info["recall"], PREC_AT_K_PCT*100, prec_at_k
        ))

        # Export de predicciones de validación (hold-out)
        METRICS_DIR.mkdir(parents=True, exist_ok=True)
        fold_df = pd.DataFrame({
            "mint": te_df["mint"].values,
            "y_true": y_te,
            "y_prob": y_pred,
            "timestamp": te_df["timestamp"].values,
        })
        fold_df["hour"] = pd.to_datetime(fold_df["timestamp"], utc=True, errors="coerce").dt.hour
        val_rows.append(fold_df)

        # Entrenamiento FINAL con TODO el dataset (o si prefieres, solo con train)
        lgb_full = lgb.Dataset(df[X_cols], df["label"])
        final_params = {**params, "metric": "auc"}
        final_num_boost_round =  int(1200)  # razonable; se puede ajustar
        final_model = lgb.train(
            final_params,
            lgb_full,
            num_boost_round=final_num_boost_round,
        )

        # Métrica CV "proxy" para compatibilidad de retorno (usamos AP hold-out)
        auc_cv_mean = float(auc)
        ap_cv_mean = float(ap)
        best_thr_f1 = float(th_info["threshold"])
        thr_backup = best_thr_f1  # en forward no hay folds; usamos el mismo como respaldo

    # 3.b) BACK-COMPAT: CV temporal agrupada por mint (si no se definió forward)
    else:
        print("[SPLIT] Hold-out forward NO configurado. Usando CV temporal agrupada por mint (back-compat).")
        cv_splits = _grouped_time_splits_by_mint(df, n_splits=5, min_pos_per_fold=1)
        aucs, aps, thresholds_j, best_iters = [], [], [], []
        params_cv = dict(**params)
        val_rows = []

        for fold, (tr_idx, te_idx) in enumerate(cv_splits, 1):
            tr_df, te_df = df.iloc[tr_idx], df.iloc[te_idx]
            y_tr, y_te = tr_df["label"].values, te_df["label"].values

            lgb_train = lgb.Dataset(tr_df[X_cols], y_tr)
            lgb_test = lgb.Dataset(te_df[X_cols], y_te)

            model = lgb.train(
                params_cv,
                lgb_train,
                num_boost_round=1200,
                valid_sets=[lgb_test],
                callbacks=[lgb.early_stopping(80, verbose=False)],
            )

            y_pred = model.predict(te_df[X_cols], num_iteration=model.best_iteration)
            try:
                auc = roc_auc_score(y_te, y_pred)
            except Exception:
                auc = float("nan")
            ap = average_precision_score(y_te, y_pred) if y_te.sum() > 0 else 0.0

            aucs.append(auc)
            aps.append(ap)
            best_iters.append(model.best_iteration or 200)

            # Youden J (respaldo por fold)
            try:
                fpr, tpr, thr = roc_curve(y_te, y_pred)
                j_stat = tpr - fpr
                best_thr = float(thr[np.argmax(j_stat)])
            except Exception:
                best_thr = 0.5
            thresholds_j.append(best_thr)

            # recolecta filas de validación
            fold_df = pd.DataFrame({
                "mint": df_raw.iloc[te_idx]["mint"].values,
                "y_true": y_te.astype(int),
                "y_prob": y_pred,
                "timestamp": df_raw.iloc[te_idx]["timestamp"].values,
            })
            fold_df["hour"] = pd.to_datetime(fold_df["timestamp"], utc=True, errors="coerce").dt.hour
            fold_df["fold"] = fold
            val_rows.append(fold_df)

            print(f"[CV] Fold{fold}: AUC={auc:.4f}  AP={ap:.4f}  it*={model.best_iteration}")

        auc_cv_mean = float(np.nanmean(aucs))
        ap_cv_mean = float(np.nanmean(aps))
        print(f"[CV] Mean AUC={auc_cv_mean:.4f} ± {np.nanstd(aucs):.4f}")
        print(f"[CV] Mean AP ={ap_cv_mean:.4f} ± {np.nanstd(aps):.4f}")

        # Entrenamiento final con TODO el dataset
        lgb_full = lgb.Dataset(df[X_cols], df["label"])
        final_num_boost_round = int(np.median(best_iters) if best_iters else 600)
        final_model = lgb.train(
            {**params, "metric": "auc"},
            lgb_full,
            num_boost_round=final_num_boost_round,
        )

        # Umbral respaldo como mediana de Youden
        thr_backup = float(np.median(thresholds_j)) if len(thresholds_j) else 0.5

        # Umbral F1 se calculará sobre TODAS las val (concatenadas) más abajo
        best_thr_f1 = None  # se define tras concatenar val_rows

    # 4) Exportar predicciones de validación y calcular umbral recomendado por F1
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    val_preds = pd.concat(val_rows, ignore_index=True)
    # Orden bonito de columnas
    ordered = ["mint", "y_true", "y_prob", "timestamp", "hour"] + ([c for c in ("fold",) if "fold" in val_preds.columns])
    val_preds = val_preds[ordered]
    val_preds.to_csv(VAL_PREDS_CSV, index=False)
    print(f"[ML] Predicciones de validación → {VAL_PREDS_CSV}  (rows={len(val_preds)})")

    th_info_all = _best_threshold_f1(val_preds["y_true"].to_numpy(), val_preds["y_prob"].to_numpy())
    thr_recommended = float(th_info_all["threshold"])
    # Si veníamos de forward, best_thr_f1 ya existía; priorizamos el calculado sobre todas las val (debe ser mismo conjunto)
    best_thr_f1 = thr_recommended

    # Precision@K sobre todas las val
    prec_at_k_all = _precision_at_k(val_preds["y_true"].to_numpy(), val_preds["y_prob"].to_numpy(), k_pct=PREC_AT_K_PCT)

    print(
        "[THR] Recomendado (F1) threshold={:.4f}  F1={:.4f}  P={:.4f}  R={:.4f}  AP={:.4f}  Prec@{:.0f}%={:.4f}".format(
            th_info_all["threshold"], th_info_all["f1"], th_info_all["precision"],
            th_info_all["recall"], th_info_all["ap"], PREC_AT_K_PCT*100, prec_at_k_all
        )
    )

    # 5) Importancias de variables (top-N)
    try:
        importances = final_model.feature_importance()
        feat_imp = sorted(zip(X_cols, importances), key=lambda x: -x[1])
        top_n = min(15, len(feat_imp))
        print("[IMP] Top features:")
        for name, imp in feat_imp[:top_n]:
            print(f"      {name:30s}  {imp:.1f}")
    except Exception as e:
        print(f"[IMP] No se pudieron calcular importancias: {e}")

    # 6) Suavizado de umbral (si hay uno previo y MIN_THR_CHANGE>0)
    prev_thr: Optional[float] = None
    if RECOMMENDED_JSON.exists():
        try:
            prev = json.loads(RECOMMENDED_JSON.read_text())
            prev_thr = float(prev.get("picked"))
        except Exception:
            prev_thr = None

    applied_thr = thr_recommended
    applied_reason = "new"
    if prev_thr is not None and MIN_THR_CHANGE > 0.0:
        if abs(thr_recommended - prev_thr) < MIN_THR_CHANGE:
            applied_thr = prev_thr
            applied_reason = f"smoothing(Δ<{MIN_THR_CHANGE})"
    print(f"[THR] Aplicado={applied_thr:.4f}  (prev={prev_thr}  rec={thr_recommended:.4f}  reason={applied_reason})")

    # 7) Estimación simple de P&L simulado (opcional)
    pnl_est = _simple_pnl_estimate(
        val_preds["y_true"].to_numpy(),
        val_preds["y_prob"].to_numpy(),
        thr=applied_thr,
    )
    print(f"[PNL] Estimado (simple): buys={pnl_est['buys']}  wins={pnl_est['wins']}  fails={pnl_est['fails']}  pnl≈{pnl_est['pnl']:.2f}  (win={SIM_PNL_PER_WIN}, fail={SIM_PNL_PER_FAIL})")

    # 8) Persistencia atómica del modelo
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=MODEL_PATH.parent,
        prefix=".tmp_model_",
        suffix=".pkl",
    )
    os.close(tmp_fd)
    joblib.dump(final_model, tmp_path)
    pathlib.Path(tmp_path).replace(MODEL_PATH)

    # 9) recommended_threshold.json (con info ampliada y smoothing)
    RECOMMENDED_JSON.parent.mkdir(parents=True, exist_ok=True)
    RECOMMENDED_JSON.write_text(json.dumps({
        "picked": applied_thr,
        "picked_reason": applied_reason,
        "recommended_raw": thr_recommended,
        "metric": "max_f1_on_val_preds",
        "precision_at_thr": th_info_all["precision"],
        "recall_at_thr": th_info_all["recall"],
        "f1_at_thr": th_info_all["f1"],
        "ap_val": th_info_all["ap"],
        "precision_at_k_pct": PREC_AT_K_PCT,
        "precision_at_k_val": prec_at_k_all,
        "backup_threshold_you_d_en_median": float(thr_backup),
        "pnl_estimate": pnl_est,
    }, indent=2))
    print(f"[ML] Umbral recomendado → {RECOMMENDED_JSON}")

    # 10) Metadatos del modelo
    META_PATH.write_text(json.dumps({
        "auc_cv_mean_or_forward": float(auc_cv_mean),
        "auc_pr_mean_or_forward": float(ap_cv_mean),
        "ai_threshold_recommended": float(thr_recommended),
        "ai_threshold_applied": float(applied_thr),
        "threshold_metric": "max_f1_on_val_preds",
        "splits":      int(1 if use_forward else 5),
        "rows":        int(len(df)),
        "features":    X_cols,
        "feature_set_hash": feat_hash,
        "model_path":  str(MODEL_PATH),
    }, indent=2))
    print(f"[ML] Modelo + meta guardados en {MODEL_PATH}")

    return float(auc_cv_mean)


if __name__ == "__main__":
    train_and_save()
