from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from config.config import CFG


WARNING_IN_SAMPLE_ONLY = "in_sample_only"
WARNING_NOT_ENOUGH_ROWS = "not_enough_rows"
WARNING_SINGLE_CLASS = "single_class"
WARNING_LOW_PRECISION_AT_K = "low_precision_at_k"
WARNING_UNSTABLE_BY_LANE = "unstable_by_lane"
WARNING_NOT_READY_FOR_ENFORCEMENT = "not_ready_for_enforcement"
RANKING_METRIC_VERSION = "expected_boundary_ties_v1"
RANKING_TOKEN_SKILL_VERSION = "fit_cohort_token_time_capture_v3"
RANKING_SELECTION_SCOPE = "original_score_fit_cohort_topk"

CRITICAL_MODEL_WARNINGS = {
    WARNING_IN_SAMPLE_ONLY,
    WARNING_NOT_ENOUGH_ROWS,
    WARNING_SINGLE_CLASS,
    WARNING_LOW_PRECISION_AT_K,
    WARNING_UNSTABLE_BY_LANE,
    WARNING_NOT_READY_FOR_ENFORCEMENT,
}


def ranking_at_k(y_true: Any, scores: Any, *, k_pct: float | None = None) -> dict[str, Any]:
    """Expected top-k metrics, with equal treatment of exact boundary ties.

    All rows strictly above the cutoff are selected. Remaining places are
    shared fractionally across the whole equal-score boundary group. This is
    the expectation under label-independent random tie breaking, not a random
    execution policy or proof of financial returns. Non-tied ranks keep their
    existing rounded-k, at-least-one selection rule.
    """
    result = {"version": RANKING_METRIC_VERSION, "grain": "observed_target_row",
              "rows": 0, "positives": 0, "k": 0, "k_pct": None,
              "precision": None, "recall": None, "cutoff_score": None,
              "strictly_above_rows": 0, "boundary_tied_rows": 0,
              "boundary_selected_weight": None, "expected_true_positives": None}
    try:
        truth, pred = np.asarray(y_true, dtype=float), np.asarray(scores, dtype=float)
        pct = float(k_pct if k_pct is not None else getattr(CFG, "PRECISION_AT_K_PCT", 0.10))
        if (truth.ndim != 1 or pred.shape != truth.shape or not truth.size
                or not np.isfinite(pct) or not np.isin(truth, [0, 1]).all()):
            return result
        finite = np.isfinite(pred)
        truth, pred = truth[finite], pred[finite]
        if not truth.size:
            return result
        pct = max(min(pct, 1.), 0.)
        k = max(1, int(round(truth.size * pct)))
        cutoff = float(np.partition(pred, len(pred) - k)[len(pred) - k])
        above, tied = pred > cutoff, pred == cutoff
        above_count, tied_count = int(above.sum()), int(tied.sum())
        weight = (k - above_count) / tied_count
        positives = int(truth.sum())
        true_positives = float(truth[above].sum() + weight * truth[tied].sum())
        result.update(rows=len(truth), positives=positives, k=k, k_pct=pct,
                      precision=true_positives / k,
                      recall=true_positives / positives if positives else None,
                      cutoff_score=cutoff, strictly_above_rows=above_count,
                      boundary_tied_rows=tied_count, boundary_selected_weight=weight,
                      expected_true_positives=true_positives)
    except (TypeError, ValueError, OverflowError):
        pass
    return result


def precision_at_k(y_true: Any, scores: Any, *, k_pct: float | None = None) -> float | None:
    return ranking_at_k(y_true, scores, k_pct=k_pct)["precision"]


def _score_cohort_selection(y_true, scores, score_cohorts, *, k_pct):
    """Scores from different fitted models never compete for one top-k budget."""
    y, pred = np.asarray(y_true, dtype=float), np.asarray(scores, dtype=float)
    if (y.ndim != 1 or pred.shape != y.shape or not len(y)
            or not np.isin(y, [0, 1]).all() or not np.isfinite(pred).all()):
        raise ValueError("Incomplete cohort ranking values")
    cohorts = pd.Series(["0"] * len(y) if score_cohorts is None else score_cohorts,
                        dtype="string").reset_index(drop=True).str.strip()
    if len(cohorts) != len(y) or cohorts.isna().any() or cohorts.eq("").any():
        raise ValueError("Incomplete score fit cohorts")
    weights, capacity = np.zeros(len(y)), np.zeros(len(y))
    records = []
    for identity in sorted(cohorts.unique()):
        mask = cohorts.eq(identity).to_numpy(dtype=bool)
        ranking = ranking_at_k(y[mask], pred[mask], k_pct=k_pct)
        if ranking["rows"] != int(mask.sum()) or ranking["precision"] is None:
            raise ValueError("Invalid cohort ranking")
        weights[mask] = np.where(pred[mask] > ranking["cutoff_score"], 1.,
            np.where(pred[mask] == ranking["cutoff_score"], ranking["boundary_selected_weight"], 0.))
        capacity[mask] = ranking["k"] / ranking["rows"]
        records.append({"cohort_id": str(identity), **ranking})
    k = sum(record["k"] for record in records)
    # Integer positive counts and exact tie weights are already deterministic
    # per cohort. Summing those summaries avoids row-order floating drift.
    true_positives = float(sum(record["expected_true_positives"] for record in records))
    metrics = {"version": RANKING_METRIC_VERSION, "grain": "observed_target_row",
               "selection_scope": RANKING_SELECTION_SCOPE, "rows": len(y),
               "positives": int(y.sum()), "k": k, "k_pct": records[0]["k_pct"],
               "precision": true_positives / k, "recall": true_positives / float(y.sum()) if y.sum() else None,
               "expected_true_positives": true_positives, "score_cohorts": records}
    return y, weights, capacity, metrics


def ranking_across_score_cohorts(y_true: Any, scores: Any, score_cohorts: Any, *,
                                k_pct: float | None = None) -> dict[str, Any]:
    """Aggregate actual per-model-fit top-k selections, not pooled raw scores."""
    try:
        return _score_cohort_selection(y_true, scores, score_cohorts, k_pct=k_pct)[3]
    except (TypeError, ValueError, OverflowError):
        return {"version": RANKING_METRIC_VERSION, "grain": "observed_target_row",
                "selection_scope": RANKING_SELECTION_SCOPE, "rows": 0, "positives": 0,
                "k": 0, "k_pct": None, "precision": None, "recall": None,
                "expected_true_positives": None, "score_cohorts": []}


def ranking_token_skill(y_true: Any, scores: Any, tokens: Any, *,
                        baseline_scores: Any = None, k_pct: float | None = None,
                        score_cohorts: Any = None, decision_times: Any = None) -> dict[str, Any]:
    """Historical paired capture utility, with one mean per case-sensitive mint.

    At the observed label-independent top-k cutoff, capture utility is y*w,
    where w is the exact-boundary selection weight. The uniform-capacity
    baseline is y*k/n within each original score fit cohort; a supplied
    incumbent uses its own same-capacity cutoff in those same cohorts.
    Omitted cohorts mean one fitted scorer, not a pooled walk-forward run.
    Token and fixed one-hour bucket sensitivity checks hold observed cuts
    fixed. Missing original decision times never create temporal support.
    This is not full dependence validation, future coverage or a buy gate.
    """
    from ml.prediction_validation import (paired_token_loss_check, paired_token_time_block_check)

    comparison = "uniform_capacity" if baseline_scores is None else "incumbent_topk"
    result = {**paired_token_loss_check([], [], []),
              "version": RANKING_TOKEN_SKILL_VERSION,
              "ranking_metric_version": RANKING_METRIC_VERSION,
              "selection_scope": RANKING_SELECTION_SCOPE,
              "score_cohort_count": 0, "score_cohort_capacity": [],
              "comparison": comparison, "grain": "case_sensitive_token_mean_capture",
              "positive_tokens": 0, "capacity_fraction": None,
              "mean_capture_per_token": None, "mean_baseline_capture_per_token": None,
              "capture_lift": None, "reason": "invalid_or_incomplete_ranking_evidence"}
    result.update(token_cluster_validation_ready=False,
                  temporal_support=paired_token_time_block_check([], None, [], [], []))
    try:
        y, selected, capacity_by_row, ranking = _score_cohort_selection(
            y_true, scores, score_cohorts, k_pct=k_pct)
        ids = pd.Series(tokens, dtype="string").reset_index(drop=True).str.strip()
        if len(ids) != len(y) or ids.isna().any() or ids.eq("").any():
            return result
        capture = y * selected
        capacity = ranking["k"] / len(y)
        baseline_capture = y * capacity_by_row
        if baseline_scores is not None:
            _, old_selected, old_capacity, _ = _score_cohort_selection(
                y, baseline_scores, score_cohorts, k_pct=k_pct)
            if not np.array_equal(old_capacity, capacity_by_row):
                return result
            baseline_capture = y * old_selected
        check = paired_token_loss_check(ids, 1. - capture, 1. - baseline_capture)
        temporal = paired_token_time_block_check(ids, decision_times, 1. - capture, 1. - baseline_capture, y)
        grouped = pd.DataFrame({"token": ids, "truth": y, "capture": capture,
                                "baseline": baseline_capture}).groupby("token", sort=True).mean()
        mean_capture, mean_baseline = float(grouped.capture.mean()), float(grouped.baseline.mean())
        lift = mean_capture / mean_baseline if mean_baseline > 0 else None
        positive_tokens = int(grouped.truth.gt(0).sum())
        ready = bool(check["validation_ready"] and temporal["validation_ready"] and positive_tokens >= 5
                     and (comparison != "uniform_capacity" or lift is not None and lift >= 1.25))
        reason = check["reason"]
        if check["validation_ready"] and not ready:
            reason = "insufficient_positive_token_support" if positive_tokens < 5 else (
                temporal["reason"] if not temporal["validation_ready"] else "insufficient_token_capture_lift")
        result.update(check, positive_tokens=positive_tokens, capacity_fraction=capacity,
                      token_cluster_validation_ready=check["validation_ready"], temporal_support=temporal,
                      score_cohort_count=len(ranking["score_cohorts"]),
                      score_cohort_capacity=[{key: record[key] for key in ("cohort_id", "rows", "k")}
                                             for record in ranking["score_cohorts"]],
                      mean_capture_per_token=mean_capture, mean_baseline_capture_per_token=mean_baseline,
                      capture_lift=lift, validation_ready=ready, reason=reason)
    except (TypeError, ValueError, OverflowError):
        pass
    return result


def ranking_token_skill_ready(payload: Any, *, comparison: str = "uniform_capacity") -> bool:
    """Reject legacy or incomplete declarations without inventing approvals."""
    from ml.prediction_validation import token_time_block_skill_ready
    if (not isinstance(payload, dict) or payload.get("version") != RANKING_TOKEN_SKILL_VERSION
            or payload.get("ranking_metric_version") != RANKING_METRIC_VERSION
            or payload.get("method") != "paired_token_cluster_bootstrap"
            or payload.get("grain") != "case_sensitive_token_mean_capture"
            or payload.get("selection_scope") != RANKING_SELECTION_SCOPE
            or payload.get("comparison") != comparison or payload.get("validation_ready") is not True
            or type(payload.get("unique_tokens")) is not int or payload["unique_tokens"] < 30
            or type(payload.get("positive_tokens")) is not int or payload["positive_tokens"] < 5
            or payload.get("bootstrap_samples") != 1000 or payload.get("lower_quantile") != .05
            or payload.get("token_cluster_validation_ready") is not True
            or not token_time_block_skill_ready(payload.get("temporal_support"))):
        return False
    lower, lift = payload.get("lower_loss_improvement"), payload.get("capture_lift")
    captured, baseline = payload.get("mean_capture_per_token"), payload.get("mean_baseline_capture_per_token")
    gain, capacity = payload.get("mean_loss_improvement"), payload.get("capacity_fraction")
    if (any(type(value) not in (int, float) or not np.isfinite(value)
            for value in (lower, captured, baseline, gain, capacity))
            or not 0 <= captured <= 1 or not 0 <= baseline <= 1 or not 0 < capacity <= 1
            or lower <= 0 or gain <= 0 or not np.isclose(gain, captured - baseline, rtol=1e-9, atol=1e-12)):
        return False
    temporal = payload["temporal_support"]
    if (any(temporal[key] != payload[key] for key in ("rows", "unique_tokens", "positive_tokens"))
            or not np.isclose(temporal["mean_loss_improvement"], gain, rtol=1e-9, atol=1e-12)):
        return False
    cohorts = payload.get("score_cohort_capacity")
    if (type(payload.get("score_cohort_count")) is not int or not isinstance(cohorts, list)
            or not cohorts or len(cohorts) != payload["score_cohort_count"]
            or type(payload.get("rows")) is not int or payload["rows"] < payload["unique_tokens"]):
        return False
    identities = []
    for cohort in cohorts:
        if (not isinstance(cohort, dict) or type(cohort.get("cohort_id")) is not str
                or not cohort["cohort_id"].strip() or cohort["cohort_id"] != cohort["cohort_id"].strip()
                or type(cohort.get("rows")) is not int or type(cohort.get("k")) is not int
                or not 1 <= cohort["k"] <= cohort["rows"]):
            return False
        identities.append(cohort["cohort_id"])
    if (len(set(identities)) != len(identities) or sum(c["rows"] for c in cohorts) != payload["rows"]
            or not np.isclose(sum(c["k"] for c in cohorts) / payload["rows"], capacity, rtol=1e-9, atol=1e-12)):
        return False
    if baseline == 0:
        return comparison == "incumbent_topk" and lift is None
    return bool(type(lift) in (int, float) and np.isfinite(lift)
                and np.isclose(lift, captured / baseline, rtol=1e-9, atol=1e-12)
                and (comparison != "uniform_capacity" or lift >= 1.25))


def lane_stability_warning(frame: pd.DataFrame, target: str | None = None, *, min_lane_rows: int = 10) -> tuple[bool, dict[str, Any]]:
    if "entry_lane" not in frame.columns:
        return True, {"reason": "missing_entry_lane"}
    lane = frame["entry_lane"].fillna("unknown").astype(str)
    counts = lane.value_counts()
    small_lanes = {str(key): int(value) for key, value in counts.items() if int(value) < int(min_lane_rows)}
    payload: dict[str, Any] = {
        "lane_count": int(len(counts)),
        "small_lanes": small_lanes,
        "min_lane_rows": int(min_lane_rows),
    }
    if len(counts) < 2:
        payload["reason"] = "single_lane"
        return True, payload
    if target and target in frame.columns:
        y = pd.to_numeric(frame[target], errors="coerce").fillna(0).astype(int)
        by_lane = frame.assign(_label=y).groupby(lane)["_label"].agg(["count", "sum"]).to_dict(orient="index")
        payload["by_lane"] = {str(key): {"count": int(value["count"]), "positives": int(value["sum"])} for key, value in by_lane.items()}
        if any(int(value["sum"]) == 0 or int(value["sum"]) == int(value["count"]) for value in by_lane.values() if int(value["count"]) >= min_lane_rows):
            payload["reason"] = "single_class_lane"
            return True, payload
    if small_lanes:
        payload["reason"] = "small_lanes"
        return True, payload
    return False, payload


def target_validation_payload(
    *,
    warnings: list[str] | tuple[str, ...],
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    unique = sorted(set(warnings) | {WARNING_NOT_READY_FOR_ENFORCEMENT})
    critical = sorted(set(unique) & CRITICAL_MODEL_WARNINGS)
    return {
        "warnings": unique,
        "critical_warnings": critical,
        "ready_for_enforcement": False,
        **(dict(details or {})),
    }


def collect_report_warnings(payload: Any) -> dict[str, Any]:
    warnings: set[str] = set()
    critical: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key in ("warnings", "critical_warnings"):
                items = value.get(key)
                if isinstance(items, list):
                    target = critical if key == "critical_warnings" else warnings
                    target.update(str(item) for item in items)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    critical.update(warnings & CRITICAL_MODEL_WARNINGS)
    return {
        "warnings": sorted(warnings),
        "critical_warnings": sorted(critical),
        "has_critical_warnings": bool(critical),
        "ready_for_enforcement": False if critical else bool((payload or {}).get("ready_for_enforcement", False)) if isinstance(payload, Mapping) else False,
    }


__all__ = [
    "CRITICAL_MODEL_WARNINGS",
    "WARNING_IN_SAMPLE_ONLY",
    "WARNING_LOW_PRECISION_AT_K",
    "WARNING_NOT_ENOUGH_ROWS",
    "WARNING_NOT_READY_FOR_ENFORCEMENT",
    "WARNING_SINGLE_CLASS",
    "WARNING_UNSTABLE_BY_LANE",
    "collect_report_warnings",
    "lane_stability_warning",
    "precision_at_k",
    "ranking_at_k",
    "RANKING_METRIC_VERSION",
    "RANKING_TOKEN_SKILL_VERSION",
    "ranking_token_skill",
    "ranking_token_skill_ready",
    "ranking_across_score_cohorts",
    "target_validation_payload",
]
