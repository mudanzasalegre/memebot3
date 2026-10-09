"""Token-cluster diagnostics for advisory predictions, never profit guarantees."""
from __future__ import annotations

from typing import Any
from datetime import datetime

import numpy as np
import pandas as pd

TOKEN_TIME_BLOCK_VERSION = "paired_token_hour_blocks_v1"
TIME_BLOCK_SECONDS = 3600
TIME_BLOCK_OFFSETS = (0, 1800)
MIN_TIME_BLOCKS = 5
MIN_POSITIVE_TIME_BLOCKS = 3


def paired_token_time_block_check(tokens: Any, decision_times: Any, model_loss: Any,
                                  baseline_loss: Any, positives: Any) -> dict[str, Any]:
    """Historical burst-support sensitivity, not independent-market coverage.

    Each mint contributes one mean delta, anchored at its earliest original
    OOS decision. Whole UTC hour buckets are resampled together, at two fixed
    origins, so many tokens in one burst cannot become many bootstrap units.
    Selected top-k cuts stay fixed. Neither the one-hour horizon nor these
    overlapping sensitivity checks proves stationarity, creator independence
    or dependence beyond an hour. No block length is optimized on outcomes.
    """
    result = {"version": TOKEN_TIME_BLOCK_VERSION, "method": "paired_token_time_bucket_bootstrap",
        "anchor": "earliest_original_oos_decision_per_token", "block_seconds": TIME_BLOCK_SECONDS,
        "offsets_seconds": list(TIME_BLOCK_OFFSETS), "minimum_blocks": MIN_TIME_BLOCKS,
        "minimum_positive_blocks": MIN_POSITIVE_TIME_BLOCKS, "bootstrap_samples": 1000,
        "lower_quantile": .05, "rows": 0, "unique_tokens": 0, "positive_tokens": 0,
        "mean_loss_improvement": None, "lower_loss_improvement": None, "schemes": [],
        "validation_ready": False, "reason": "missing_or_invalid_original_decision_times",
        "scope": "historical_one_hour_burst_sensitivity_not_full_dependence_or_financial_acceptance"}
    try:
        ids = pd.Series(tokens, dtype="string").reset_index(drop=True).str.strip()
        raw_times = pd.Series(decision_times).reset_index(drop=True)
        actual, baseline, labels = (np.asarray(value, dtype=float)
                                    for value in (model_loss, baseline_loss, positives))
        if (any(value.ndim != 1 or len(value) != len(ids) for value in (actual, baseline, labels))
                or not len(ids) or len(raw_times) != len(ids) or ids.isna().any() or ids.eq("").any()
                or not np.isfinite(actual).all() or not np.isfinite(baseline).all()
                or np.any(actual < 0) or np.any(baseline < 0) or not np.isin(labels, [0, 1]).all()
                or not raw_times.map(lambda value: isinstance(value, (str, datetime, pd.Timestamp, np.datetime64))).all()):
            return result
        times = pd.to_datetime(raw_times, utc=True, errors="coerce", format="mixed")
        if times.isna().any():
            return result
        grouped = pd.DataFrame({"token": ids, "delta": baseline - actual,
            "decision_time": times, "positive": labels}).groupby("token", sort=True).agg(
                delta=("delta", "mean"), decision_time=("decision_time", "min"), positive=("positive", "max"))
        if not np.isfinite(grouped.delta).all():
            return result
        mean = float(grouped.delta.mean())
        result.update(rows=len(ids), unique_tokens=len(grouped), positive_tokens=int(grouped.positive.sum()),
                      mean_loss_improvement=mean)
        # UTC nanosecond ticks avoid float rounding around a bucket boundary.
        ticks = grouped.decision_time.astype("datetime64[ns, UTC]").array.asi8
        schemes = []
        for offset in TIME_BLOCK_OFFSETS:
            buckets = (ticks - offset * 1_000_000_000) // (TIME_BLOCK_SECONDS * 1_000_000_000)
            units = grouped.assign(bucket=buckets).groupby("bucket", sort=True).agg(
                delta_sum=("delta", "sum"), tokens=("delta", "size"), positive_tokens=("positive", "sum"))
            sums, counts = units.delta_sum.to_numpy(), units.tokens.to_numpy()
            support = int(units.positive_tokens.gt(0).sum())
            record = {"offset_seconds": offset, "blocks": len(units), "positive_blocks": support,
                "block_support": [{"bucket_id": int(bucket), "tokens": int(row.tokens),
                    "positive_tokens": int(row.positive_tokens), "delta_sum": float(row.delta_sum)}
                    for bucket, row in units.iterrows()], "lower_loss_improvement": None,
                "validation_ready": False}
            if len(units) >= MIN_TIME_BLOCKS and support >= MIN_POSITIVE_TIME_BLOCKS:
                rng = np.random.default_rng(782)
                means = []
                for _ in range(20):
                    drawn = rng.integers(0, len(units), size=(50, len(units)))
                    means.append(sums[drawn].sum(axis=1) / counts[drawn].sum(axis=1))
                lower = float(np.quantile(np.concatenate(means), .05))
                record.update(lower_loss_improvement=lower, validation_ready=bool(lower > 0))
            schemes.append(record)
        token_support = len(grouped) >= 30 and result["positive_tokens"] >= 5
        ready = token_support and mean > 0 and all(record["validation_ready"] for record in schemes)
        lower_values = [record["lower_loss_improvement"] for record in schemes]
        result.update(schemes=schemes, validation_ready=ready,
            lower_loss_improvement=min(lower_values) if all(value is not None for value in lower_values) else None,
            reason="historical_time_bucket_skill_supported" if ready else
                "insufficient_token_support" if not token_support else
                "insufficient_time_bucket_support" if any(record["blocks"] < MIN_TIME_BLOCKS or
                    record["positive_blocks"] < MIN_POSITIVE_TIME_BLOCKS for record in schemes) else
                "time_bucket_skill_uncertain_or_nonpositive")
    except (TypeError, ValueError, OverflowError):
        pass
    return result


def token_time_block_skill_ready(payload: Any) -> bool:
    """Require complete typed current declarations, not a legacy ready flag."""
    if (not isinstance(payload, dict) or payload.get("version") != TOKEN_TIME_BLOCK_VERSION
            or payload.get("method") != "paired_token_time_bucket_bootstrap"
            or payload.get("anchor") != "earliest_original_oos_decision_per_token"
            or type(payload.get("block_seconds")) is not int or payload["block_seconds"] != TIME_BLOCK_SECONDS
            or payload.get("offsets_seconds") != list(TIME_BLOCK_OFFSETS)
            or any(type(value) is not int for value in payload["offsets_seconds"])
            or type(payload.get("minimum_blocks")) is not int or payload["minimum_blocks"] != MIN_TIME_BLOCKS
            or type(payload.get("minimum_positive_blocks")) is not int or payload["minimum_positive_blocks"] != MIN_POSITIVE_TIME_BLOCKS
            or type(payload.get("bootstrap_samples")) is not int or payload["bootstrap_samples"] != 1000
            or type(payload.get("lower_quantile")) is not float or payload["lower_quantile"] != .05
            or payload.get("validation_ready") is not True
            or type(payload.get("rows")) is not int or type(payload.get("unique_tokens")) is not int
            or type(payload.get("positive_tokens")) is not int
            or not 30 <= payload["unique_tokens"] <= payload["rows"]
            or not 5 <= payload["positive_tokens"] <= payload["unique_tokens"]):
        return False
    mean, lower = payload.get("mean_loss_improvement"), payload.get("lower_loss_improvement")
    if any(type(value) not in (int, float) or not np.isfinite(value) or value <= 0 for value in (mean, lower)):
        return False
    schemes = payload.get("schemes")
    if not isinstance(schemes, list) or len(schemes) != len(TIME_BLOCK_OFFSETS):
        return False
    lower_values = []
    for scheme, offset in zip(schemes, TIME_BLOCK_OFFSETS):
        if (not isinstance(scheme, dict) or type(scheme.get("offset_seconds")) is not int
                or scheme["offset_seconds"] != offset or scheme.get("validation_ready") is not True
                or type(scheme.get("blocks")) is not int or scheme["blocks"] < MIN_TIME_BLOCKS
                or type(scheme.get("positive_blocks")) is not int or scheme["positive_blocks"] < MIN_POSITIVE_TIME_BLOCKS):
            return False
        records = scheme.get("block_support")
        value = scheme.get("lower_loss_improvement")
        if (not isinstance(records, list) or len(records) != scheme["blocks"]
                or type(value) not in (int, float) or not np.isfinite(value) or value <= 0):
            return False
        seen = []
        for record in records:
            if (not isinstance(record, dict) or type(record.get("bucket_id")) is not int
                    or type(record.get("tokens")) is not int or record["tokens"] < 1
                    or type(record.get("positive_tokens")) is not int
                    or not 0 <= record["positive_tokens"] <= record["tokens"]
                    or type(record.get("delta_sum")) not in (int, float) or not np.isfinite(record["delta_sum"])):
                return False
            seen.append(record["bucket_id"])
        if (len(set(seen)) != len(seen) or seen != sorted(seen)
                or sum(record["tokens"] for record in records) != payload["unique_tokens"]
                or sum(record["positive_tokens"] for record in records) != payload["positive_tokens"]
                or sum(record["positive_tokens"] > 0 for record in records) != scheme["positive_blocks"]
                or not np.isclose(sum(record["delta_sum"] for record in records) / payload["unique_tokens"], mean,
                                  rtol=1e-9, atol=1e-12)):
            return False
        lower_values.append(value)
    return bool(np.isclose(min(lower_values), lower, rtol=1e-9, atol=1e-12))


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


__all__ = ["paired_token_loss_check", "regression_error_check", "paired_token_time_block_check",
           "token_time_block_skill_ready", "TOKEN_TIME_BLOCK_VERSION"]
