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
    "target_validation_payload",
]
