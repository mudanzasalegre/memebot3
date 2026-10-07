from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import mean_absolute_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.base import clone
from sklearn.metrics import brier_score_loss

from config.config import CFG, PROJECT_ROOT
from ml.feature_matrix import coerce_feature_frame
from features.context_encoding import (augment_context_frame, available_context_features,
    context_encoding_schema, FEATURE_SOURCES)
from ml.feature_sets import feature_set, feature_set_hash
from ml.label_builder import attach_labels
from ml.outcome_targets import enrich_outcome_targets
from ml.temporal_validation import purged_temporal_windows, temporal_eligibility
from ml.calibrated_ranker import fit_calibrated_ranker, reliability_bins
from ml.prediction_validation import paired_token_loss_check, regression_error_check
from ml.financial_targets import checked_financial_frame, financial_target
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


def _recall_at_k(y_true: Any, scores: Any, *, k_pct: float | None = None) -> float | None:
    truth = np.asarray(y_true, dtype=int)
    pred = np.asarray(scores, dtype=float)
    if truth.size == 0 or pred.size == 0 or truth.size != pred.size:
        return None
    finite = np.isfinite(pred)
    truth = truth[finite]
    pred = pred[finite]
    positives = int((truth > 0).sum())
    if truth.size == 0 or positives <= 0:
        return None
    pct = float(k_pct if k_pct is not None else getattr(CFG, "PRECISION_AT_K_PCT", 0.10))
    k = max(1, int(round(truth.size * max(min(pct, 1.0), 0.0))))
    order = np.argsort(pred)[::-1][:k]
    return float((truth[order] > 0).sum() / positives)


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
    return attach_labels(enrich_outcome_targets(df, PROJECT_ROOT))


def _settled_training_frame(df: pd.DataFrame) -> pd.DataFrame:
    valid, *_ = temporal_eligibility(df)
    # Legacy untimed fixtures can produce diagnostic models, but no temporal
    # validation or runtime role. A partially timed dataset must not contaminate
    # a validated final artifact with unobserved/future labels.
    if valid.any():
        df = df.loc[valid].copy()
    return df.reset_index(drop=True)


def _forward_predictions(df, X, y, model, *, min_rows: int, classifier: bool):
    windows, details = purged_temporal_windows(df, min_train_rows=min_rows)
    truths, predictions, positions = [], [], []
    evaluated_folds = []
    probability_truth, probability_predictions, baseline_predictions = [], [], []
    probability_tokens, regression_baselines = [], []
    _, _, _, identities = temporal_eligibility(df)
    for train, test in windows:
        if classifier and y.iloc[train].nunique() < 2:
            continue
        calibration = None
        if classifier:
            candidate, calibration = fit_calibrated_ranker(model, X.iloc[train].reset_index(drop=True), y.iloc[train].reset_index(drop=True),
                                                          df.iloc[train].reset_index(drop=True), min_rows=min_rows)
            pred = candidate.rank_score(X.iloc[test])
            if calibration["calibrated"]:
                probability_truth.extend(y.iloc[test].tolist())
                probability_predictions.extend(candidate.predict_proba(X.iloc[test])[:, 1].tolist())
                baseline_predictions.extend([float(y.iloc[train].mean())] * len(test))
                probability_tokens.extend(identities.iloc[test].tolist())
        else:
            candidate = clone(model).fit(X.iloc[train], y.iloc[train])
            pred = candidate.predict(X.iloc[test])
            regression_baselines.extend([float(y.iloc[train].median())] * len(test))
        truths.extend(y.iloc[test].tolist())
        predictions.extend(pred.tolist())
        positions.extend(test.tolist())
        evaluated_folds.append({"train_rows": len(train), "test_rows": len(test), "calibration": calibration})
    details["evaluated_folds"] = evaluated_folds
    details["out_of_sample_rows"] = len(truths)
    details["out_of_sample_unique_tokens"] = int(identities.iloc[positions].nunique())
    if classifier:
        brier = float(brier_score_loss(probability_truth, probability_predictions)) if probability_truth else None
        baseline_brier = float(brier_score_loss(probability_truth, baseline_predictions)) if probability_truth else None
        details["probability_evaluation"] = {
            "rows": len(probability_truth), "positives": int(sum(probability_truth)),
            "brier_score": brier, "baseline_brier_score": baseline_brier,
            "brier_skill_score": 1 - brier / baseline_brier if baseline_brier is not None and baseline_brier > 0 else None,
            "baseline": "Each outer fold's mature training prevalence, not the future test prevalence",
            "reliability_bins": reliability_bins(probability_truth, probability_predictions),
            "positive_tokens": len({token for token, label in zip(probability_tokens, probability_truth) if label == 1}),
            "negative_tokens": len({token for token, label in zip(probability_tokens, probability_truth) if label == 0}),
            "cluster_skill": paired_token_loss_check(
                probability_tokens, (np.asarray(probability_truth) - np.asarray(probability_predictions)) ** 2,
                (np.asarray(probability_truth) - np.asarray(baseline_predictions)) ** 2),
        }
    else:
        details["regression_evaluation"] = regression_error_check(
            identities.iloc[positions].tolist(), truths, predictions, regression_baselines)
    return np.asarray(truths), np.asarray(predictions), np.asarray(positions, dtype=int), details


def _save_family_model(model, path: Path, metadata: dict[str, Any]) -> None:
    """Atomic files plus checksum let readers reject a mixed model/meta pair."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    meta_path = path.with_suffix(".meta.json")
    meta_tmp = meta_path.with_name(f".{meta_path.name}.{uuid4().hex}.tmp")
    try:
        joblib.dump(model, temporary)
        payload = {**metadata, "model_sha256": sha256(temporary.read_bytes()).hexdigest(),
                   "context_encoding": context_encoding_schema(metadata.get("features") or [])}
        meta_tmp.write_text(json.dumps(_json_safe(payload), indent=2, allow_nan=False), encoding="utf-8")
        os.replace(temporary, path)
        os.replace(meta_tmp, meta_path)
    finally:
        temporary.unlink(missing_ok=True)
        meta_tmp.unlink(missing_ok=True)


def _export_validation_predictions(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        frame.to_csv(temporary, index=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def train_classifier_family(
    *,
    family: str,
    targets: list[str],
    feature_set_name: str,
    frame: pd.DataFrame | None = None,
    output_dir: Path | None = None,
    min_rows: int = 20,
    validation_predictions_path: Path | None = None,
    financial_target_parameters: dict[str, float] | None = None,
) -> dict[str, Any]:
    if validation_predictions_path is not None and len(targets) != 1:
        raise ValueError("validation_prediction_export_requires_one_target")
    df = _settled_training_frame(load_training_frame(frame))
    financial = None
    if family == "risk":
        df, financial = checked_financial_frame(df)
        df = attach_labels(df)
        if "severe_loss_configured" in targets:
            threshold = (financial_target_parameters or {}).get("severe_loss_pct")
            if threshold is None or not np.isfinite(threshold) or not -100 <= threshold < 0:
                raise ValueError("configured_risk_requires_explicit_net_target_definition")
            df["severe_loss_configured"] = df["target_total_pnl_pct"].le(threshold).astype("Int64")
    df = augment_context_frame(df, available_context_features(df))
    features = list(dict.fromkeys(column for column in feature_set(feature_set_name) if column in df.columns))
    report: dict[str, Any] = {
        "family": family,
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "feature_set": feature_set_name,
        "feature_set_hash": feature_set_hash(feature_set_name),
        "rows": int(len(df)),
        "financial_training": financial,
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
        observed = pd.to_numeric(df[target], errors="coerce")
        mask = observed.isin([0, 1])
        target_df = df.loc[mask].reset_index(drop=True)
        target_X = X.loc[mask].reset_index(drop=True)
        y = observed.loc[mask].astype(int).reset_index(drop=True)
        if len(y) < min_rows:
            report["targets"][target] = {
                "status": "skipped", "reason": "not_enough_observed_target_rows",
                "target_rows": len(y), "unlabelled_rows": int((~mask).sum()),
                "validation": target_validation_payload(warnings=[WARNING_NOT_ENOUGH_ROWS]),
            }
            continue
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
        model = Pipeline(
            steps=[
                ("scaler", StandardScaler()),
                ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
            ]
        )
        truth, pred, positions, temporal = _forward_predictions(target_df, target_X, y, model, min_rows=min_rows, classifier=True)
        model, calibration = fit_calibrated_ranker(model, target_X, y, target_df, min_rows=min_rows)
        p_at_k = precision_at_k(truth, pred)
        r_at_k = _recall_at_k(truth, pred)
        target_warnings = [WARNING_NOT_READY_FOR_ENFORCEMENT]
        if not len(pred):
            target_warnings.append(WARNING_IN_SAMPLE_ONLY)
        precision_floor = float(getattr(CFG, "ML_TUNE_PRECISION_FLOOR", 0.60) or 0.60)
        if p_at_k is None or float(p_at_k) < precision_floor:
            target_warnings.append(WARNING_LOW_PRECISION_AT_K)
        unstable, lane_details = lane_stability_warning(target_df.iloc[positions] if len(positions) else target_df, target)
        if unstable:
            target_warnings.append(WARNING_UNSTABLE_BY_LANE)
        model_path = target_dir / f"{target}.pkl"
        probability_eval = temporal["probability_evaluation"]
        skill = probability_eval.get("brier_skill_score")
        probability_ready = bool(calibration["calibrated"] and probability_eval["rows"] >= 30
                                 and probability_eval["positive_tokens"] >= 5 and probability_eval["negative_tokens"] >= 5
                                 and probability_eval["cluster_skill"]["validation_ready"]
                                 and skill is not None and skill > 0)
        lift = p_at_k / float(np.mean(truth)) if p_at_k is not None and len(truth) and np.mean(truth) > 0 else None
        positive_tokens = temporal_eligibility(target_df)[3].iloc[positions][truth == 1].nunique() if len(truth) else 0
        ranking_ready = bool(temporal["out_of_sample_unique_tokens"] >= 30 and positive_tokens >= 5
                             and lift is not None and lift >= 1.25)
        report["targets"][target] = {
            "status": "trained",
            "model_path": str(model_path),
            "positives": int(y.sum()),
            "target_rows": len(y),
            "financial_training": checked_financial_frame(target_df)[1] if financial is not None else None,
            "financial_target_parameters": financial_target_parameters if financial is not None else None,
            "unlabelled_rows": int((~mask).sum()),
            "avg_pred": float(np.mean(pred)) if len(pred) else None,
            "base_rate": float(np.mean(truth)) if len(truth) else None,
            "precision_lift_at_k": lift,
            "brier_score": probability_eval["brier_score"],
            "baseline_brier_score": probability_eval["baseline_brier_score"],
            "brier_skill_score": skill,
            "probabilities_calibrated": bool(calibration["calibrated"]),
            "prediction_kind": "calibrated_binary_probability",
            "probability_validation_ready": probability_ready,
            "ranking_validation_ready": ranking_ready,
            "calibration": calibration,
            "rank_reference_quantiles": np.quantile(model.rank_score(target_X), np.linspace(0, 1, 101)).tolist(),
            "probability_caveat": "Ranking scores are separate from held-out calibrated event probabilities. Neither proves executable or costed profit, and prospective validation is required before sizing or exits change.",
            "precision_at_k": p_at_k,
            "recall_at_k": r_at_k,
            "precision_at_k_pct": float(getattr(CFG, "PRECISION_AT_K_PCT", 0.10) or 0.10),
            "features": features,
            "validation": target_validation_payload(
                warnings=target_warnings,
                details={
                    "mode": "purged_token_walk_forward" if len(pred) else "in_sample_only",
                    "temporal": temporal,
                    "precision_floor": precision_floor,
                    "lane_stability": lane_details,
                },
            ),
        }
        _save_family_model(model, model_path, {**report["targets"][target], "family": family, "target": target,
                           "trained_at_utc": report["trained_at_utc"], "feature_set_hash": report["feature_set_hash"],
                           "use": "advisory_only", "automatic_live_activation": False})
        if validation_predictions_path is not None:
            export = target_df.iloc[positions].copy()
            export = export.assign(y_true=truth, rank_score=pred)
            # A rank score is not a calibrated probability. The CSV makes the
            # actual OOS role explicit and cannot masquerade as fit-row returns.
            export = export[[column for column in ("mint", "address", "timestamp", "outcome_closed_at", "ts",
                                                   "target_total_pnl_pct", "y_true", "rank_score") if column in export]]
            _export_validation_predictions(export, validation_predictions_path)
    report["status"] = "ok"
    report["data_quality"] = df.attrs.get("outcome_target_join", {})
    target_warnings = [warning for item in report["targets"].values() for warning in item.get("validation", {}).get("warnings", [])]
    report["validation"] = target_validation_payload(warnings=target_warnings, details={"mode": "target_specific"})
    return _json_safe(report)


def train_regressor_family(
    *,
    family: str,
    targets: list[str],
    feature_set_name: str,
    frame: pd.DataFrame | None = None,
    output_dir: Path | None = None,
    min_rows: int = 20,
    validation_predictions_path: Path | None = None,
    financial_target_parameters: dict[str, float] | None = None,
) -> dict[str, Any]:
    if validation_predictions_path is not None and len(targets) != 1:
        raise ValueError("validation_prediction_export_requires_one_target")
    df = _settled_training_frame(load_training_frame(frame))
    # Mixed opportunity/financial target lists are handled independently below.
    df = augment_context_frame(df, available_context_features(df))
    features = list(dict.fromkeys(column for column in feature_set(feature_set_name) if column in df.columns))
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
        target_source = df
        financial = None
        if financial_target(family, target):
            target_source, financial = checked_financial_frame(df)
            target_source = attach_labels(target_source)
            if target == "ev_configured_clipped":
                parameters = financial_target_parameters or {}
                low, high = parameters.get("clip_min"), parameters.get("clip_max")
                if low is None or high is None or not np.isfinite([low, high]).all() or low >= high:
                    raise ValueError("configured_ev_requires_explicit_net_target_definition")
                target_source[target] = target_source["target_total_pnl_pct"].clip(low, high)
        y = pd.to_numeric(target_source[target], errors="coerce")
        mask = y.notna() & np.isfinite(y)
        if int(mask.sum()) < min_rows:
            report["targets"][target] = {
                "status": "skipped",
                "reason": "not_enough_target_rows",
                "financial_training": financial,
                "validation": target_validation_payload(
                    warnings=[WARNING_IN_SAMPLE_ONLY, WARNING_NOT_ENOUGH_ROWS, WARNING_NOT_READY_FOR_ENFORCEMENT],
                    details={"mode": "in_sample_only", "target_rows": int(mask.sum()), "min_rows": int(min_rows)},
                ),
            }
            continue
        model = RandomForestRegressor(n_estimators=50, max_depth=5, random_state=42, min_samples_leaf=5)
        target_df = target_source.loc[mask].reset_index(drop=True)
        target_X = coerce_feature_frame(target_df, features)
        target_y = y.loc[mask].reset_index(drop=True)
        truth, pred, positions, temporal = _forward_predictions(target_df, target_X, target_y, model, min_rows=min_rows, classifier=False)
        model.fit(target_X, target_y)
        model_path = target_dir / f"{target}.pkl"
        unstable, lane_details = lane_stability_warning(target_df.iloc[positions] if len(positions) else target_df, None)
        target_warnings = [WARNING_NOT_READY_FOR_ENFORCEMENT]
        if not len(pred):
            target_warnings.append(WARNING_IN_SAMPLE_ONLY)
        if unstable:
            target_warnings.append(WARNING_UNSTABLE_BY_LANE)
        report["targets"][target] = {
            "status": "trained",
            "model_path": str(model_path),
            "mae": float(mean_absolute_error(truth, pred)) if len(pred) else None,
            "prediction_kind": "regression_pct_points",
            "regression_validation_ready": bool(temporal["regression_evaluation"]["validation_ready"]),
            "regression_evaluation": temporal["regression_evaluation"],
            "target_rows": len(target_y), "unlabelled_rows": int((~mask).sum()),
            "financial_training": checked_financial_frame(target_df)[1] if financial is not None else None,
            "financial_target_parameters": financial_target_parameters if financial is not None else None,
            "features": features,
            "validation": target_validation_payload(
                warnings=target_warnings,
                details={"mode": "purged_token_walk_forward" if len(pred) else "in_sample_only", "temporal": temporal, "lane_stability": lane_details},
            ),
        }
        _save_family_model(model, model_path, {**report["targets"][target], "family": family, "target": target,
                           "trained_at_utc": report["trained_at_utc"], "feature_set_hash": report["feature_set_hash"],
                           "use": "advisory_only", "automatic_live_activation": False})
        if validation_predictions_path is not None:
            export = target_df.iloc[positions].copy().assign(target_ev=truth, ev_pred_pct=pred)
            export = export[[column for column in ("mint", "address", "timestamp", "outcome_closed_at", "ts",
                                                   "target_total_pnl_pct", "target_ev", "ev_pred_pct") if column in export]]
            _export_validation_predictions(export, validation_predictions_path)
    report["status"] = "ok"
    report["data_quality"] = df.attrs.get("outcome_target_join", {})
    target_warnings = [warning for item in report["targets"].values() for warning in item.get("validation", {}).get("warnings", [])]
    report["validation"] = target_validation_payload(warnings=target_warnings, details={"mode": "target_specific"})
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
    df = augment_context_frame(df, available_context_features(df))
    features = [column for column in feature_set("exit_features") if column in df.columns
                and column != "exit_profile" and FEATURE_SOURCES.get(column) != "exit_profile"]
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
