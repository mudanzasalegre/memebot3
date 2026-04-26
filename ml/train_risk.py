from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd
from sklearn.linear_model import LogisticRegression

from config.config import CFG, PROJECT_ROOT
from ml.feature_matrix import coerce_feature_frame
from ml.risk_model import risk_summary, severe_loss_labels
from ml.train import _filter_outcome_training_rows, _load_dataset, _select_feature_columns

MODEL_PATH = PROJECT_ROOT / "ml" / "risk_model.pkl"
META_PATH = PROJECT_ROOT / "ml" / "risk_model.meta.json"
VAL_PREDS = PROJECT_ROOT / "data" / "metrics" / "risk_val_preds.csv"
THRESHOLDS_JSON = PROJECT_ROOT / "data" / "metrics" / "risk_thresholds.json"


def train_risk_model() -> dict:
    df = _load_dataset()
    df, _meta = _filter_outcome_training_rows(df, entry_lane_allowlist=getattr(CFG, "ML_BOOTSTRAP_ENTRY_LANE_ALLOWLIST", ""), dex_allowlist=getattr(CFG, "ML_BOOTSTRAP_DEX_ALLOWLIST", ""))
    if df.empty:
        raise ValueError("risk model requires outcome rows")
    df["label"] = severe_loss_labels(df, severe_loss_pct=float(getattr(CFG, "ML_SEVERE_LOSS_PCT", -30.0)))
    df, x_cols, excluded = _select_feature_columns(df)
    if int(df["label"].sum()) <= 0 or int((1 - df["label"]).sum()) <= 0:
        raise ValueError("risk model requires both classes")
    X = coerce_feature_frame(df, x_cols)
    y = df["label"].astype(int)
    model = LogisticRegression(max_iter=1000, class_weight="balanced")
    model.fit(X, y)
    proba = model.predict_proba(X)[:, 1]
    VAL_PREDS.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"mint": df["mint"].values, "y_true": y.values, "y_prob": proba, "target_total_pnl_pct": df["target_total_pnl_pct"].values}).to_csv(VAL_PREDS, index=False)
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    meta = {
        "trained_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": f"target_total_pnl_pct <= {getattr(CFG, 'ML_SEVERE_LOSS_PCT', -30.0)}",
        "features": x_cols,
        "excluded_columns": excluded,
        "rows": int(len(df)),
        "positives": int(y.sum()),
    }
    META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    thresholds = risk_summary(y, proba, threshold=float(getattr(CFG, "ML_RISK_VETO_THRESHOLD", 0.70)))
    THRESHOLDS_JSON.write_text(json.dumps(thresholds, indent=2), encoding="utf-8")
    return meta


if __name__ == "__main__":
    print(json.dumps(train_risk_model(), indent=2))
