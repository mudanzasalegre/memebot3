"""Checked primary probabilities: later calibration and separate outer evidence."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any

import numpy as np
import pandas as pd

from ml.calibrated_ranker import CalibratedRanker, fit_calibrated_ranker, reliability_bins
from ml.prediction_validation import paired_token_loss_check
from ml.temporal_validation import temporal_eligibility

VERSION = "entry_temporal_probability_v1"
EVALUATION_MODE = "purged_outer_probability_evaluation"


def _digest(details: dict[str, Any]) -> str:
    return sha256(json.dumps(details, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class PrimaryCalibratedRanker(CalibratedRanker):
    """The calibrator stays attached to the exact base estimator it calibrated."""
    def __init__(self, ranker: CalibratedRanker, calibration: dict[str, Any]):
        super().__init__(ranker.base_model, ranker.calibrated_model)
        self.entry_probability_version_ = VERSION
        self.calibration_ = deepcopy(calibration)
        self.calibration_sha256_ = _digest(calibration)


def fit_primary_probability(model: Any, X: pd.DataFrame, y: pd.Series, frame: pd.DataFrame):
    X, y, frame = X.reset_index(drop=True), y.reset_index(drop=True), frame.reset_index(drop=True)
    valid, *_ = temporal_eligibility(frame)
    if len(X) != len(y) or len(y) != len(frame) or not valid.all() or not len(y):
        raise ValueError("Primary calibration requires known settled T0/label times and token identities")
    if not y.isin([0, 1]).all() or y.nunique() != 2:
        raise ValueError("Primary calibration requires both binary classes")
    ranker, details = fit_calibrated_ranker(model, X, y, frame, min_rows=20, min_class_count=3)
    return PrimaryCalibratedRanker(ranker, details)


def checked_outer_boundary(train: pd.DataFrame, test: pd.DataFrame) -> dict[str, Any]:
    tr_valid, tr_times, available, tr_tokens = temporal_eligibility(train)
    te_valid, te_times, _, te_tokens = temporal_eligibility(test)
    if train.empty or test.empty or not tr_valid.all() or not te_valid.all():
        raise ValueError("Outer probability evaluation requires known settled observations")
    start = te_times.min()
    if (set(tr_tokens) & set(te_tokens) or not (tr_times < start).all()
            or not (available < start - pd.Timedelta(seconds=60)).all()):
        raise ValueError("Outer probability evaluation must be later, token-disjoint and label-purged")
    return {"label_availability_purged": True, "token_disjoint": True,
            "train_rows": len(train), "test_rows": len(test), "embargo_seconds": 60.,
            "train_label_latest": available.max().isoformat(), "test_start": start.isoformat()}


def evaluate_probabilities(predictions: pd.DataFrame, folds: list[dict[str, Any]]) -> dict[str, Any]:
    truth = predictions.y_true.to_numpy(dtype=float)
    probability = predictions.y_prob.to_numpy(dtype=float)
    baseline = predictions.baseline_probability.to_numpy(dtype=float)
    if (not len(truth) or not np.isin(truth, [0, 1]).all()
            or not np.isfinite(probability).all() or not np.isfinite(baseline).all()
            or np.any((probability < 0) | (probability > 1))
            or np.any((baseline < 0) | (baseline > 1))):
        raise ValueError("Invalid primary OOS probabilities or mature training baseline")
    loss, reference = (truth - probability) ** 2, (truth - baseline) ** 2
    brier, baseline_brier = float(loss.mean()), float(reference.mean())
    tokens = predictions.mint.astype("string")
    cluster = paired_token_loss_check(tokens, loss, reference)
    positive_tokens = int(tokens[truth == 1].nunique())
    negative_tokens = int(tokens[truth == 0].nunique())
    skill = 1 - brier / baseline_brier if baseline_brier > 0 else None
    ready = bool(len(truth) >= 30 and positive_tokens >= 5 and negative_tokens >= 5
                 and cluster["validation_ready"] and skill is not None and skill > 0)
    return {"mode": EVALUATION_MODE, "rows": len(truth), "positives": int(truth.sum()),
            "unique_tokens": int(tokens.nunique()), "positive_tokens": positive_tokens,
            "negative_tokens": negative_tokens, "brier_score": brier,
            "baseline_brier_score": baseline_brier, "brier_skill_score": skill,
            "baseline": "Each outer fold's mature training prevalence, never the future test prevalence",
            "cluster_skill": cluster, "reliability_bins": reliability_bins(truth, probability),
            "folds": deepcopy(folds), "validation_ready": ready,
            "scope": "historical_net_event_probability_not_prospective_strategy_profit"}


def probability_metadata(model: Any, evaluation: dict[str, Any]) -> dict[str, Any]:
    calibration = deepcopy(getattr(model, "calibration_", {}))
    calibrated = isinstance(model, PrimaryCalibratedRanker) and model.calibrated_model is not None
    return {"probability_contract_version": VERSION, "prediction_kind": "calibrated_binary_probability",
            "probabilities_calibrated": calibrated,
            "probability_validation_ready": bool(calibrated and evaluation.get("validation_ready") is True),
            "calibration": calibration, "calibration_sha256": _digest(calibration),
            "probability_evaluation": deepcopy(evaluation)}


def _count(value: Any, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and np.isfinite(value)


def _boundary_supported(boundary: dict[str, Any]) -> bool:
    latest = pd.to_datetime(boundary.get("train_label_latest"), utc=True, errors="coerce")
    start = pd.to_datetime(boundary.get("test_start"), utc=True, errors="coerce")
    embargo = boundary.get("embargo_seconds")
    return bool(pd.notna(latest) and pd.notna(start) and _finite(embargo) and embargo >= 60
                and latest < start - pd.Timedelta(seconds=embargo))


def _calibration_supported(cal: dict[str, Any]) -> bool:
    timing = cal.get("timing") or {}
    folds = [fold for fold in timing.get("folds") or [] if fold.get("used") is True]
    return bool(cal.get("calibrated") is True and cal.get("method") == "sigmoid"
                and _count(cal.get("min_class_count"), 3) and _count(cal.get("fit_rows"), 20)
                and _count(cal.get("calibration_rows"), 12)
                and timing.get("mode") == "purged_token_walk_forward" and folds
                and folds[-1].get("used") is True
                and _boundary_supported({**folds[-1], "embargo_seconds": timing.get("embargo_seconds")})
                and folds[-1].get("train_rows") == cal["fit_rows"]
                and folds[-1].get("test_rows") == cal["calibration_rows"]
                and _count(cal.get("fit_positives"), 3) and cal["fit_rows"] - cal["fit_positives"] >= 3
                and _count(cal.get("calibration_positives"), 3)
                and cal["calibration_rows"] - cal["calibration_positives"] >= 3
                and all(_count((cal.get(name) or {}).get(label, (cal.get(name) or {}).get(str(label))),
                               cal["min_class_count"])
                        and (cal.get(name) or {}).get(label, (cal.get(name) or {}).get(str(label)))
                        <= (cal[positive] if label == 1 else cal[rows] - cal[positive])
                        for name, rows, positive in (("fit_tokens_by_class", "fit_rows", "fit_positives"),
                            ("calibration_tokens_by_class", "calibration_rows", "calibration_positives"))
                        for label in (0, 1)))


def _fold_supported(fold: dict[str, Any]) -> bool:
    outer, cal = fold["outer_boundary"], fold["calibration"]
    if (outer.get("label_availability_purged") is not True or outer.get("token_disjoint") is not True
            or not _count(outer.get("train_rows"), 20) or not _count(outer.get("test_rows"), 1)
            or not _boundary_supported(outer) or not _calibration_supported(cal)
            or cal["fit_rows"] + cal["calibration_rows"] > outer["train_rows"]):
        return False
    inner = [item for item in cal["timing"]["folds"] if item.get("used") is True][-1]
    end = pd.to_datetime(inner.get("test_end"), utc=True, errors="coerce")
    return bool(pd.notna(end) and end < pd.to_datetime(outer["test_start"], utc=True))


def supported_entry_probability(metadata: dict[str, Any]) -> bool:
    """Flags alone cannot turn an uncalibrated rank or in-sample result into a probability."""
    try:
        cal, evaluation = metadata.get("calibration") or {}, metadata.get("probability_evaluation") or {}
        if (metadata.get("probability_contract_version") != VERSION
                or metadata.get("prediction_kind") != "calibrated_binary_probability"
                or metadata.get("probabilities_calibrated") is not True
                or metadata.get("probability_validation_ready") is not True
                or not _calibration_supported(cal) or metadata.get("calibration_sha256") != _digest(cal)
                or evaluation.get("mode") != EVALUATION_MODE or evaluation.get("validation_ready") is not True):
            return False
        if not all(_count(evaluation.get(name), minimum) for name, minimum in (
                ("rows", 30), ("unique_tokens", 30), ("positive_tokens", 5), ("negative_tokens", 5))):
            return False
        rows, unique = evaluation["rows"], evaluation["unique_tokens"]
        positives = evaluation.get("positives")
        if (not _count(positives, 5) or rows - positives < 5 or unique > rows
                or evaluation["positive_tokens"] > positives or evaluation["negative_tokens"] > rows - positives
                or any(evaluation[name] > unique for name in ("positive_tokens", "negative_tokens"))):
            return False
        brier, baseline, skill = (evaluation.get(name) for name in
                                  ("brier_score", "baseline_brier_score", "brier_skill_score"))
        if (not all(_finite(value) for value in (brier, baseline, skill))
                or not 0 <= brier < baseline <= 1 or not skill > 0
                or not np.isclose(skill, 1 - brier / baseline, rtol=1e-9, atol=1e-12)):
            return False
        cluster = evaluation.get("cluster_skill") or {}
        if (cluster.get("method") != "paired_token_cluster_bootstrap" or cluster.get("validation_ready") is not True
                or cluster.get("unique_tokens") != unique or cluster.get("rows") != rows
                or any(not _finite(cluster.get(name)) or cluster[name] <= 0
                       for name in ("mean_loss_improvement", "lower_loss_improvement"))):
            return False
        folds = evaluation.get("folds") or []
        return bool(folds and sum(fold["outer_boundary"]["test_rows"] for fold in folds) == rows
                    and all(_fold_supported(fold) for fold in folds))
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False


def supported_entry_model(model: Any, metadata: dict[str, Any]) -> bool:
    try:
        if not isinstance(model, PrimaryCalibratedRanker) or model.calibrated_model is None:
            return False
        calibrated = model.calibrated_model
        fitted = calibrated.calibrated_classifiers_
        configured_base = getattr(calibrated.estimator, "estimator", calibrated.estimator)
        return bool(supported_entry_probability(metadata) and isinstance(model, PrimaryCalibratedRanker)
                    and model.entry_probability_version_ == VERSION and model.calibrated_model is not None
                    and list(model.classes_) == [0, 1]
                    and list(model.base_model.classes_) == [0, 1] and list(calibrated.classes_) == [0, 1]
                    and configured_base is model.base_model and len(fitted) == 1
                    and getattr(fitted[0].estimator, "estimator", fitted[0].estimator) is model.base_model
                    and model.calibration_sha256_ == metadata["calibration_sha256"]
                    and _digest(model.calibration_) == metadata["calibration_sha256"])
    except (AttributeError, TypeError, ValueError):
        return False
