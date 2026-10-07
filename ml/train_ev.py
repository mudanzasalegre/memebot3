from __future__ import annotations

import json
import math

import joblib
import pandas as pd

from config.config import CFG, PROJECT_ROOT
from ml.family_training import _save_family_model, train_regressor_family
from ml.train import _filter_outcome_training_rows, _load_dataset

MODEL_PATH = PROJECT_ROOT / "ml" / "ev_model.pkl"
META_PATH = PROJECT_ROOT / "ml" / "ev_model.meta.json"
VAL_PREDS = PROJECT_ROOT / "data" / "metrics" / "ev_val_preds.csv"


def train_ev_model(*, frame: pd.DataFrame | None = None, min_rows: int = 20) -> dict:
    df = frame.copy() if frame is not None else _load_dataset()
    if frame is None:
        df, _meta = _filter_outcome_training_rows(df, entry_lane_allowlist=getattr(CFG, "ML_BOOTSTRAP_ENTRY_LANE_ALLOWLIST", ""), dex_allowlist=getattr(CFG, "ML_BOOTSTRAP_DEX_ALLOWLIST", ""))
    if df.empty:
        raise ValueError("EV model requires outcome rows")
    clip_min = float(getattr(CFG, "ML_EV_CLIP_MIN", -100.0))
    clip_max = float(getattr(CFG, "ML_EV_CLIP_MAX", 300.0))
    if not math.isfinite(clip_min) or not math.isfinite(clip_max) or clip_min >= clip_max:
        raise ValueError("invalid_ev_clip_range")
    values = pd.to_numeric(df.get("target_total_pnl_pct", pd.Series(float("nan"), index=df.index)), errors="coerce")
    # Reject infinity before clipping: it is not an observed giant winner.
    values = values.where(values.map(lambda value: pd.notna(value) and math.isfinite(float(value))))
    target = "ev_configured_clipped"
    df[target] = values.clip(clip_min, clip_max)
    report = train_regressor_family(family="ev", targets=[target], feature_set_name="ev_features", frame=df,
                                   output_dir=MODEL_PATH.parent / "models" / "ev_compatibility", min_rows=min_rows,
                                   validation_predictions_path=VAL_PREDS,
                                   financial_target_parameters={"clip_min": clip_min, "clip_max": clip_max})
    result = report.get("targets", {}).get(target, {})
    published = result.get("status") == "trained" and result.get("regression_validation_ready") is True
    if published:
        model = joblib.load(result["model_path"])
        _save_family_model(model, MODEL_PATH, {**result, "family": "ev", "target": target,
                           "trained_at_utc": report["trained_at_utc"], "clip_min": clip_min, "clip_max": clip_max,
                           "use": "advisory_only", "automatic_live_activation": False})
    return {**report, "compatibility_published": bool(published), "clip_min": clip_min, "clip_max": clip_max}


if __name__ == "__main__":
    print(json.dumps(train_ev_model(), indent=2))
