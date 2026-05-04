from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import mean_absolute_error

from config.config import CFG, PROJECT_ROOT
from ml.feature_matrix import coerce_feature_frame
from ml.feature_sets import feature_set, feature_set_hash
from ml.label_builder import attach_labels
from ml.train import _filter_outcome_training_rows, _load_dataset
from ml.model_validation_warnings import (
    WARNING_IN_SAMPLE_ONLY,
    WARNING_LOW_PRECISION_AT_K,
    WARNING_NOT_ENOUGH_ROWS,
    WARNING_NOT_READY_FOR_ENFORCEMENT,
    WARNING_SINGLE_CLASS,
    WARNING_UNSTABLE_BY_LANE,
    lane_stability_warning,
    precision_at_k,
    target_validation_payload,
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value_f = float(value)
        if not np.isfinite(value_f):
            return None
        return value_f
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def load_training_frame(frame: pd.DataFrame | None = None) -> pd.DataFrame:
    if frame is not None:
        return attach_labels(frame.copy())
    df = _load_dataset()
    df, _meta = _filter_outcome_training_rows(df)
    return attach_labels(df)


def train_classifier_family(
    *,
    family: str,
    targets: list[str],
    feature_set_name: str,
    frame: pd.DataFrame | None = None,
    output_dir: Path | None = None,
    min_rows: int = 20,
) -> dict[str, Any]:
    df = load_training_frame(frame)
    features = [column for column in feature_set(feature_set_name) if column in df.columns]
    report: dict[str, Any] = {
        "family": family,
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "feature_set": feature_set_name,
        "feature_set_hash": feature_set_hash(feature_set_name),
        "rows": int(len(df)),
        "targets": {},
        "validation": target_validation_payload(
            warnings=[WARNING_IN_SAMPLE_ONLY, WARNING_NOT_READY_FOR_ENFORCEMENT],
            details={"mode": "in_sample_only"},
        ),
    }
    if len(df) < min_rows or not features:
        report["status"] = "skipped"
        report["reason"] = "not_enough_rows_or_features"
        report["validation"] = target_validation_payload(
            warnings=[WARNING_IN_SAMPLE_ONLY, WARNING_NOT_ENOUGH_ROWS, WARNING_NOT_READY_FOR_ENFORCEMENT],
            details={"mode": "in_sample_only", "min_rows": int(min_rows), "feature_count": len(features)},
        )
        return report
    X = coerce_feature_frame(df, features)
    target_dir = output_dir or PROJECT_ROOT / "ml" / "models" / family
    target_dir.mkdir(parents=True, exist_ok=True)
    for target in targets:
        if target not in df.columns:
            report["targets"][target] = {
                "status": "skipped",
                "reason": "missing_target",
                "validation": target_validation_payload(
                    warnings=[WARNING_NOT_ENOUGH_ROWS, WARNING_NOT_READY_FOR_ENFORCEMENT],
                ),
            }
            continue
        y = pd.to_numeric(df[target], errors="coerce").fillna(0).astype(int)
        if y.nunique() < 2:
            report["targets"][target] = {
                "status": "skipped",
                "reason": "single_class",
                "positives": int(y.sum()),
                "validation": target_validation_payload(
                    warnings=[WARNING_IN_SAMPLE_ONLY, WARNING_SINGLE_CLASS, WARNING_NOT_READY_FOR_ENFORCEMENT],
                    details={"mode": "in_sample_only"},
                ),
            }
            continue
        model = LogisticRegression(max_iter=1000, class_weight="balanced")
        model.fit(X, y)
        pred = model.predict_proba(X)[:, 1]
        p_at_k = precision_at_k(y.values, pred)
        target_warnings = [WARNING_IN_SAMPLE_ONLY, WARNING_NOT_READY_FOR_ENFORCEMENT]
        precision_floor = float(getattr(CFG, "ML_TUNE_PRECISION_FLOOR", 0.60) or 0.60)
        if p_at_k is None or float(p_at_k) < precision_floor:
            target_warnings.append(WARNING_LOW_PRECISION_AT_K)
        unstable, lane_details = lane_stability_warning(df, target)
        if unstable:
            target_warnings.append(WARNING_UNSTABLE_BY_LANE)
        model_path = target_dir / f"{target}.pkl"
        joblib.dump(model, model_path)
        report["targets"][target] = {
            "status": "trained",
            "model_path": str(model_path),
            "positives": int(y.sum()),
            "avg_pred": float(np.mean(pred)),
            "precision_at_k": p_at_k,
            "precision_at_k_pct": float(getattr(CFG, "PRECISION_AT_K_PCT", 0.10) or 0.10),
            "features": features,
            "validation": target_validation_payload(
                warnings=target_warnings,
                details={
                    "mode": "in_sample_only",
                    "precision_floor": precision_floor,
                    "lane_stability": lane_details,
                },
            ),
        }
    report["status"] = "ok"
    return _json_safe(report)


def train_regressor_family(
    *,
    family: str,
    targets: list[str],
    feature_set_name: str,
    frame: pd.DataFrame | None = None,
    output_dir: Path | None = None,
    min_rows: int = 20,
) -> dict[str, Any]:
    df = load_training_frame(frame)
    features = [column for column in feature_set(feature_set_name) if column in df.columns]
    report: dict[str, Any] = {
        "family": family,
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "feature_set": feature_set_name,
        "feature_set_hash": feature_set_hash(feature_set_name),
        "rows": int(len(df)),
        "targets": {},
        "validation": target_validation_payload(
            warnings=[WARNING_IN_SAMPLE_ONLY, WARNING_NOT_READY_FOR_ENFORCEMENT],
            details={"mode": "in_sample_only"},
        ),
    }
    if len(df) < min_rows or not features:
        report["status"] = "skipped"
        report["reason"] = "not_enough_rows_or_features"
        report["validation"] = target_validation_payload(
            warnings=[WARNING_IN_SAMPLE_ONLY, WARNING_NOT_ENOUGH_ROWS, WARNING_NOT_READY_FOR_ENFORCEMENT],
            details={"mode": "in_sample_only", "min_rows": int(min_rows), "feature_count": len(features)},
        )
        return report
    X = coerce_feature_frame(df, features)
    target_dir = output_dir or PROJECT_ROOT / "ml" / "models" / family
    target_dir.mkdir(parents=True, exist_ok=True)
    for target in targets:
        if target not in df.columns:
            report["targets"][target] = {
                "status": "skipped",
                "reason": "missing_target",
                "validation": target_validation_payload(
                    warnings=[WARNING_NOT_ENOUGH_ROWS, WARNING_NOT_READY_FOR_ENFORCEMENT],
                ),
            }
            continue
        y = pd.to_numeric(df[target], errors="coerce")
        mask = y.notna()
        if int(mask.sum()) < min_rows:
            report["targets"][target] = {
                "status": "skipped",
                "reason": "not_enough_target_rows",
                "validation": target_validation_payload(
                    warnings=[WARNING_IN_SAMPLE_ONLY, WARNING_NOT_ENOUGH_ROWS, WARNING_NOT_READY_FOR_ENFORCEMENT],
                    details={"mode": "in_sample_only", "target_rows": int(mask.sum()), "min_rows": int(min_rows)},
                ),
            }
            continue
        model = RandomForestRegressor(n_estimators=50, max_depth=5, random_state=42, min_samples_leaf=5)
        model.fit(X.loc[mask], y.loc[mask])
        pred = model.predict(X.loc[mask])
        model_path = target_dir / f"{target}.pkl"
        joblib.dump(model, model_path)
        unstable, lane_details = lane_stability_warning(df.loc[mask], None)
        target_warnings = [WARNING_IN_SAMPLE_ONLY, WARNING_NOT_READY_FOR_ENFORCEMENT]
        if unstable:
            target_warnings.append(WARNING_UNSTABLE_BY_LANE)
        report["targets"][target] = {
            "status": "trained",
            "model_path": str(model_path),
            "mae": float(mean_absolute_error(y.loc[mask], pred)),
            "features": features,
            "validation": target_validation_payload(
                warnings=target_warnings,
                details={"mode": "in_sample_only", "lane_stability": lane_details},
            ),
        }
    report["status"] = "ok"
    return _json_safe(report)


def train_exit_classifier(
    *,
    frame: pd.DataFrame | None = None,
    output_dir: Path | None = None,
    min_rows: int = 20,
) -> dict[str, Any]:
    df = load_training_frame(frame)
    if "best_exit_profile" not in df.columns:
        peak = pd.to_numeric(df.get("max_pnl_pct_seen", df.get("target_total_pnl_pct")), errors="coerce").fillna(0)
        risk = pd.to_numeric(df.get("target_total_pnl_pct"), errors="coerce").fillna(0)
        df["best_exit_profile"] = np.where(peak >= 300, "moonbag", np.where(peak >= 100, "runner", np.where(risk < -30, "defensive", "balanced")))
    features = [column for column in feature_set("exit_features") if column in df.columns and column != "exit_profile"]
    report: dict[str, Any] = {"family": "exit", "rows": int(len(df)), "targets": {}}
    if len(df) < min_rows or not features or df["best_exit_profile"].nunique() < 2:
        report["status"] = "skipped"
        report["reason"] = "not_enough_rows_features_or_classes"
        return report
    X = coerce_feature_frame(df, features)
    y = df["best_exit_profile"].astype("string")
    model = RandomForestClassifier(n_estimators=50, max_depth=5, random_state=42, min_samples_leaf=5)
    model.fit(X, y)
    target_dir = output_dir or PROJECT_ROOT / "ml" / "models" / "exit"
    target_dir.mkdir(parents=True, exist_ok=True)
    model_path = target_dir / "best_exit_profile.pkl"
    joblib.dump(model, model_path)
    return {"family": "exit", "status": "ok", "model_path": str(model_path), "rows": int(len(df)), "features": features}


__all__ = ["load_training_frame", "train_classifier_family", "train_exit_classifier", "train_regressor_family"]
