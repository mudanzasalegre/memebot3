from __future__ import annotations

import json
import math

import joblib
import pandas as pd

from config.config import CFG, PROJECT_ROOT
from ml.family_training import _save_family_model, train_classifier_family
from ml.risk_model import severe_loss_labels
from ml.train import _filter_outcome_training_rows, _load_dataset

MODEL_PATH = PROJECT_ROOT / "ml" / "risk_model.pkl"
META_PATH = PROJECT_ROOT / "ml" / "risk_model.meta.json"
VAL_PREDS = PROJECT_ROOT / "data" / "metrics" / "risk_val_preds.csv"
THRESHOLDS_JSON = PROJECT_ROOT / "data" / "metrics" / "risk_thresholds.json"


def train_risk_model(*, frame: pd.DataFrame | None = None, min_rows: int = 20) -> dict:
    df = frame.copy() if frame is not None else _load_dataset()
    if frame is None:
        df, _meta = _filter_outcome_training_rows(df, entry_lane_allowlist=getattr(CFG, "ML_BOOTSTRAP_ENTRY_LANE_ALLOWLIST", ""), dex_allowlist=getattr(CFG, "ML_BOOTSTRAP_DEX_ALLOWLIST", ""))
    if df.empty:
        raise ValueError("risk model requires outcome rows")
    threshold = float(getattr(CFG, "ML_SEVERE_LOSS_PCT", -30.0))
    if not math.isfinite(threshold) or not -100 <= threshold < 0:
        raise ValueError("invalid_severe_loss_threshold")
    veto_threshold = float(getattr(CFG, "ML_RISK_VETO_THRESHOLD", 0.70))
    if not math.isfinite(veto_threshold) or not 0 <= veto_threshold <= 1:
        raise ValueError("invalid_risk_veto_threshold")
    target = "severe_loss_configured"
    df[target] = severe_loss_labels(df, severe_loss_pct=threshold)
    report = train_classifier_family(family="risk", targets=[target], feature_set_name="risk_features",
                                    frame=df, min_rows=min_rows, output_dir=MODEL_PATH.parent / "models" / "risk_compatibility",
                                    validation_predictions_path=VAL_PREDS)
    result = report.get("targets", {}).get(target, {})
    published = result.get("status") == "trained" and result.get("probability_validation_ready") is True
    if published:
        model = joblib.load(result["model_path"])
        _save_family_model(model, MODEL_PATH, {**result, "family": "risk", "target": target,
                           "trained_at_utc": report["trained_at_utc"], "severe_loss_pct": threshold,
                           "use": "advisory_only", "automatic_live_activation": False})
    diagnostics = {"published": bool(published), "severe_loss_pct": threshold,
                   "probability_validation_ready": bool(result.get("probability_validation_ready")),
                   "threshold": veto_threshold,
                   "veto_performance": "not_established_by_training_or_rank_csv",
                   "validation": result.get("validation", {})}
    THRESHOLDS_JSON.parent.mkdir(parents=True, exist_ok=True)
    THRESHOLDS_JSON.write_text(json.dumps(diagnostics, indent=2, allow_nan=False), encoding="utf-8")
    return {**report, "compatibility_published": bool(published), "severe_loss_pct": threshold}


if __name__ == "__main__":
    print(json.dumps(train_risk_model(), indent=2))
