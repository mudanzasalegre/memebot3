"""Token-cluster diagnostics for advisory predictions, never profit guarantees."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def paired_token_loss_check(tokens: Any, model_loss: Any, baseline_loss: Any) -> dict[str, Any]:
    """Compare OOS losses against a baseline learned before each test window.

    Repeated decisions about one case-sensitive mint are one bootstrap unit.
    This is a descriptive historical uncertainty check, not future coverage.
    """
    ids = pd.Series(tokens, dtype="string").reset_index(drop=True)
    actual, baseline = np.asarray(model_loss, dtype=float), np.asarray(baseline_loss, dtype=float)
    result = {"method": "paired_token_cluster_bootstrap", "rows": len(ids), "unique_tokens": 0,
              "bootstrap_samples": 1000, "lower_quantile": 0.05,
              "mean_loss_improvement": None, "lower_loss_improvement": None,
              "validation_ready": False, "scope": "historical_oos_advisory_not_financial_acceptance"}
    if (actual.ndim != 1 or baseline.ndim != 1 or len(actual) != len(ids) or len(baseline) != len(ids)
            or not np.isfinite(actual).all() or not np.isfinite(baseline).all()
            or np.any(actual < 0) or np.any(baseline < 0)
            or ids.isna().any() or ids.str.strip().eq("").any()):
        result["reason"] = "missing_or_invalid_loss_or_identity"
        return result
    grouped = pd.DataFrame({"token": ids, "delta": baseline - actual}).groupby("token", sort=True).delta.mean()
    values = grouped.to_numpy(dtype=float)
    result["unique_tokens"] = len(values)
    if not len(values) or not np.isfinite(values).all():
        result["reason"] = "missing_or_invalid_cluster_delta"
        return result
    result["mean_loss_improvement"] = float(values.mean())
    if len(values) < 30:
        result["reason"] = "insufficient_independent_tokens"
        return result
    rng = np.random.default_rng(781)
    # Batches avoid allocating the whole resampling matrix for a large store.
    means = np.concatenate([values[rng.integers(0, len(values), size=(50, len(values)))].mean(axis=1)
                            for _ in range(20)])
    lower = float(np.quantile(means, 0.05))
    result.update(lower_loss_improvement=lower, validation_ready=bool(lower > 0),
                  reason="historical_loss_skill_supported" if lower > 0 else "loss_skill_uncertain_or_nonpositive")
    return result


def regression_error_check(tokens: Any, truth: Any, predictions: Any, baseline: Any) -> dict[str, Any]:
    y, pred, ref = (np.asarray(value, dtype=float) for value in (truth, predictions, baseline))
    result = {"rows": len(y), "mae": None, "baseline_mae": None, "mae_skill_score": None,
              "absolute_error_radius_pct_points": None, "empirical_token_coverage": 0.90,
              "interval_method": "historical_oos_token_max_absolute_error",
              "interval_caveat": "Empirical OOS error envelope; not conditional or guaranteed future coverage",
              "validation_ready": False}
    if (y.ndim != 1 or pred.shape != y.shape or ref.shape != y.shape or len(y) != len(tokens)
            or not np.isfinite(y).all() or not np.isfinite(pred).all() or not np.isfinite(ref).all()):
        result["reason"] = "invalid_regression_evidence"
        return result
    error, reference_error = np.abs(y - pred), np.abs(y - ref)
    result["cluster_skill"] = paired_token_loss_check(tokens, error, reference_error)
    if not len(y):
        result["reason"] = "missing_oos_predictions"
        return result
    result["mae"], result["baseline_mae"] = float(error.mean()), float(reference_error.mean())
    result["mae_skill_score"] = 1 - result["mae"] / result["baseline_mae"] if result["baseline_mae"] > 0 else None
    if result["cluster_skill"]["validation_ready"]:
        per_token = pd.DataFrame({"token": tokens, "error": error}).groupby("token", sort=True).error.max()
        result["absolute_error_radius_pct_points"] = float(np.quantile(per_token, 0.90, method="higher"))
        result["validation_ready"] = True
    result["reason"] = result["cluster_skill"]["reason"]
    return result


__all__ = ["paired_token_loss_check", "regression_error_check"]
