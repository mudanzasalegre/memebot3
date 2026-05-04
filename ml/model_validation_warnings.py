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

CRITICAL_MODEL_WARNINGS = {
    WARNING_IN_SAMPLE_ONLY,
    WARNING_NOT_ENOUGH_ROWS,
    WARNING_SINGLE_CLASS,
    WARNING_LOW_PRECISION_AT_K,
    WARNING_UNSTABLE_BY_LANE,
    WARNING_NOT_READY_FOR_ENFORCEMENT,
}


def precision_at_k(y_true: Any, scores: Any, *, k_pct: float | None = None) -> float | None:
    truth = np.asarray(y_true, dtype=int)
    pred = np.asarray(scores, dtype=float)
    if truth.size == 0 or pred.size == 0 or truth.size != pred.size:
        return None
    finite = np.isfinite(pred)
    truth = truth[finite]
    pred = pred[finite]
    if truth.size == 0:
        return None
    pct = float(k_pct if k_pct is not None else getattr(CFG, "PRECISION_AT_K_PCT", 0.10))
    k = max(1, int(round(truth.size * max(min(pct, 1.0), 0.0))))
    order = np.argsort(pred)[::-1][:k]
    return float(np.mean(truth[order] > 0))


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
    "target_validation_payload",
]
