"""Observed outcome-style diagnostics, not an optimal or executable exit policy."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import clone

from features.context_encoding import FEATURE_SOURCES
from ml.prediction_validation import paired_token_loss_check
from ml.temporal_validation import purged_temporal_windows, temporal_eligibility

PROFILES = frozenset({"defensive", "balanced", "runner", "moonbag", "post_partial_protected", "bird_runner"})
LABEL_CONTRACT = "observed_exit_style_v1"
PREDICTION_KIND = "multiclass_outcome_style_label"


def _observed_number(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan)
    return numeric.where(~values.map(lambda value: isinstance(value, (bool, np.bool_))))


def exit_style_labels(frame: pd.DataFrame) -> tuple[pd.Series, str]:
    """Unknown outcomes stay unknown; no realized return is invented as a peak."""
    if "best_exit_profile" in frame:
        labels = frame["best_exit_profile"].astype("string").str.strip().str.lower()
        return labels.where(labels.isin(PROFILES)), "provided_diagnostic_style"
    peak = pd.Series(np.nan, index=frame.index, dtype=float)
    for name in ("max_pnl_seen", "max_pnl_pct_seen", "peak_pnl_pct", "max_pnl_pct"):
        if name in frame:
            values = _observed_number(frame[name])
            peak = peak.fillna(values.where(values.ge(-100)))
    realized = pd.Series(np.nan, index=frame.index, dtype=float)
    for name in ("realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct"):
        if name in frame:
            realized = realized.fillna(_observed_number(frame[name]))
    labels = pd.Series(pd.NA, index=frame.index, dtype="string")
    known_small = peak.notna() & peak.lt(100) & realized.notna()
    labels.loc[known_small] = "balanced"
    labels.loc[known_small & realized.lt(-30)] = "defensive"
    labels.loc[peak.ge(100) & peak.lt(300)] = "runner"
    labels.loc[peak.ge(300)] = "moonbag"
    return labels, "observed_peak_and_realized_proxy"


def forward_exit_diagnostics(frame, matrix, labels, model, *, min_rows: int):
    windows, temporal = purged_temporal_windows(frame, min_train_rows=min_rows)
    _, _, _, identities = temporal_eligibility(frame)
    truth, predictions, baselines, positions, folds = [], [], [], [], []
    for train, test in windows:
        if labels.iloc[train].nunique() < 2:
            continue
        fitted = clone(model).fit(matrix.iloc[train], labels.iloc[train])
        prediction = fitted.predict(matrix.iloc[test])
        baseline = labels.iloc[train].mode().iloc[0]
        truth.extend(labels.iloc[test].tolist())
        predictions.extend(prediction.tolist())
        baselines.extend([baseline] * len(test))
        positions.extend(test.tolist())
        folds.append({"train_rows": len(train), "test_rows": len(test), "baseline_class": str(baseline)})
    tokens = identities.iloc[positions].tolist()
    actual_loss = (np.asarray(truth) != np.asarray(predictions)).astype(float)
    baseline_loss = (np.asarray(truth) != np.asarray(baselines)).astype(float)
    classes = sorted(labels.unique().tolist())
    support = {name: len({token for token, value in zip(tokens, truth) if value == name}) for name in classes}
    cluster = paired_token_loss_check(tokens, actual_loss, baseline_loss)
    accuracy = float(1 - actual_loss.mean()) if len(truth) else None
    baseline_accuracy = float(1 - baseline_loss.mean()) if len(truth) else None
    evaluation = {"rows": len(truth), "unique_tokens": len(set(tokens)), "classes": classes,
        "class_token_counts": support, "accuracy": accuracy, "baseline_accuracy": baseline_accuracy,
        "baseline": "Majority class of each mature outer training fold, never the future test majority",
        "cluster_skill": cluster, "scope": "historical_observed_style_not_financial_exit_acceptance",
        "validation_ready": bool(cluster["validation_ready"] and all(n >= 5 for n in support.values())
            and accuracy is not None and baseline_accuracy is not None and accuracy > baseline_accuracy)}
    temporal.update(evaluated_folds=folds, out_of_sample_rows=len(truth),
                    out_of_sample_unique_tokens=len(set(tokens)))
    return evaluation, temporal


def checked_exit_metadata(metadata: dict[str, Any]) -> bool:
    """The diagnostic role and original label meaning are mandatory."""
    try:
        classes = metadata["classes"]
        features = metadata["features"]
        evaluation = metadata["classification_evaluation"]
        temporal = metadata["validation"]["temporal"]
        cluster = evaluation["cluster_skill"]
        counts = evaluation["class_token_counts"]
        accuracy, baseline = float(evaluation["accuracy"]), float(evaluation["baseline_accuracy"])
        positive = (float(cluster["mean_loss_improvement"]), float(cluster["lower_loss_improvement"]))
        return bool(metadata.get("family") == "exit" and metadata.get("target") == "best_exit_profile"
            and metadata.get("activation_role") == "diagnostic_only"
            and metadata.get("prediction_kind") == PREDICTION_KIND
            and metadata.get("label_contract") == LABEL_CONTRACT
            and metadata.get("label_source") in {"provided_diagnostic_style", "observed_peak_and_realized_proxy"}
            and metadata.get("automatic_live_activation") is False
            and metadata.get("classification_validation_ready") is True
            and metadata["validation"].get("mode") == "purged_token_walk_forward"
            and isinstance(classes, list) and len(classes) >= 2 and classes == sorted(set(classes))
            and all(isinstance(name, str) and name in PROFILES for name in classes)
            and isinstance(features, list) and bool(features)
            and not any(name == "exit_profile" or FEATURE_SOURCES.get(name) == "exit_profile" for name in features)
            and evaluation.get("validation_ready") is True and evaluation["classes"] == classes
            and evaluation.get("scope") == "historical_observed_style_not_financial_exit_acceptance"
            and isinstance(counts, dict) and set(counts) == set(classes)
            and all(isinstance(n, int) and not isinstance(n, bool) and n >= 5 for n in counts.values())
            and evaluation["rows"] == temporal["out_of_sample_rows"] == cluster["rows"]
            and evaluation["unique_tokens"] == temporal["out_of_sample_unique_tokens"] == cluster["unique_tokens"]
            and evaluation["rows"] >= evaluation["unique_tokens"] >= 30
            and cluster.get("method") == "paired_token_cluster_bootstrap"
            and cluster.get("validation_ready") is True
            and np.isfinite([accuracy, baseline, *positive]).all()
            and 0 <= baseline < accuracy <= 1 and min(positive) > 0)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
