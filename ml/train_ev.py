from __future__ import annotations

import json
from datetime import datetime, timezone

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestRegressor

from config.config import CFG, PROJECT_ROOT
from ml.feature_matrix import coerce_feature_frame
from ml.train import _filter_outcome_training_rows, _load_dataset, _select_feature_columns

MODEL_PATH = PROJECT_ROOT / "ml" / "ev_model.pkl"
META_PATH = PROJECT_ROOT / "ml" / "ev_model.meta.json"
VAL_PREDS = PROJECT_ROOT / "data" / "metrics" / "ev_val_preds.csv"


def train_ev_model() -> dict:
    df = _load_dataset()
    df, _meta = _filter_outcome_training_rows(df, entry_lane_allowlist=getattr(CFG, "ML_BOOTSTRAP_ENTRY_LANE_ALLOWLIST", ""), dex_allowlist=getattr(CFG, "ML_BOOTSTRAP_DEX_ALLOWLIST", ""))
    if df.empty:
        raise ValueError("EV model requires outcome rows")
    target = pd.to_numeric(df.get("target_total_pnl_pct"), errors="coerce")
    clip_min = float(getattr(CFG, "ML_EV_CLIP_MIN", -100.0))
    clip_max = float(getattr(CFG, "ML_EV_CLIP_MAX", 300.0))
    df = df[target.notna()].copy()
    target = target[target.notna()].clip(clip_min, clip_max)
    df, x_cols, excluded = _select_feature_columns(df)
    X = coerce_feature_frame(df, x_cols)
    model = RandomForestRegressor(n_estimators=80, max_depth=5, random_state=42, min_samples_leaf=10)
    model.fit(X, target)
    pred = model.predict(X)
    VAL_PREDS.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"mint": df["mint"].values, "target_ev": target.values, "ev_pred_pct": pred}).to_csv(VAL_PREDS, index=False)
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    meta = {
        "trained_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": f"clip(target_total_pnl_pct, {clip_min}, {clip_max})",
        "features": x_cols,
        "excluded_columns": excluded,
        "rows": int(len(df)),
    }
    META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


if __name__ == "__main__":
    print(json.dumps(train_ev_model(), indent=2))
