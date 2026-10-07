"""Separate a chronological ranking model from its held-out calibration."""
from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.base import clone
from sklearn.calibration import CalibratedClassifierCV

from ml.temporal_validation import purged_temporal_windows


class CalibratedRanker:
    def __init__(self, base_model: Any, calibrated_model: Any = None):
        self.base_model = base_model
        self.calibrated_model = calibrated_model
        self.classes_ = np.asarray([0, 1])

    def rank_score(self, X):
        return self.base_model.predict_proba(X)[:, list(self.base_model.classes_).index(1)]

    def predict_proba(self, X):
        if self.calibrated_model is None:
            raise ValueError("Uncalibrated rank scores must not be exposed as probabilities")
        return self.calibrated_model.predict_proba(X)

    def predict(self, X):
        if self.calibrated_model is not None:
            return self.calibrated_model.predict(X)
        return self.base_model.predict(X)


def fit_calibrated_ranker(model, X, y, frame, *, min_rows: int = 20, min_class_count: int = 3):
    """Calibrator uses a later, token-disjoint, label-mature holdout.

    If that split lacks evidence, keep a ranker but never fabricate calibration.
    The outer evaluation window is not supplied here, so it cannot calibrate
    the model being evaluated on it.
    """
    windows, timing = purged_temporal_windows(frame, splits=2, min_train_rows=min_rows)
    details = {"method": "sigmoid", "calibrated": False, "min_class_count": min_class_count,
               "timing": timing, "fit_rows": len(y), "calibration_rows": 0}
    if windows:
        train, calibration = windows[-1]
        fit_counts = y.iloc[train].value_counts()
        cal_counts = y.iloc[calibration].value_counts()
        supported = len(calibration) >= 12 and all(
            int(counts.get(label, 0)) >= min_class_count for counts in (fit_counts, cal_counts) for label in (0, 1)
        )
        if supported:
            base_model = clone(model).fit(X.iloc[train], y.iloc[train])
            # FrozenEstimator exists in sklearn >=1.6; support the repository's
            # >=1.4 contract through its documented prefit equivalent.
            try:
                from sklearn.frozen import FrozenEstimator
            except ImportError:
                calibrated = CalibratedClassifierCV(base_model, method="sigmoid", cv="prefit")
            else:
                calibrated = CalibratedClassifierCV(FrozenEstimator(base_model), method="sigmoid", cv=2)
            calibrated.fit(X.iloc[calibration], y.iloc[calibration])
            details.update({"calibrated": True, "fit_rows": len(train), "calibration_rows": len(calibration),
                            "fit_positives": int(y.iloc[train].sum()), "calibration_positives": int(y.iloc[calibration].sum())})
            return CalibratedRanker(base_model, calibrated), details
        details["reason"] = "insufficient_disjoint_calibration_classes_or_rows"
    else:
        details["reason"] = "missing_mature_temporal_calibration_window"
    return CalibratedRanker(clone(model).fit(X, y)), details


def reliability_bins(truth, probabilities, *, bins: int = 10):
    """Bounded descriptive reliability data, not confidence intervals."""
    truth, probabilities = np.asarray(truth), np.asarray(probabilities)
    rows = []
    if not len(truth):
        return rows
    groups = np.minimum((probabilities * bins).astype(int), bins - 1)
    for group in range(bins):
        mask = groups == group
        if mask.any():
            rows.append({"bin_lower": group / bins, "bin_upper": (group + 1) / bins,
                         "rows": int(mask.sum()), "mean_probability": float(probabilities[mask].mean()),
                         "observed_rate": float(truth[mask].mean())})
    return rows


__all__ = ["CalibratedRanker", "fit_calibrated_ranker", "reliability_bins"]
