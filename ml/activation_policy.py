from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from config.config import CFG
from ml.lane_taxonomy import LIVE_PROFIT_LANES, RESEARCH_LANES, normalize_entry_lane


def _num(name: str, default: float) -> float:
    try:
        return float(getattr(CFG, name, default))
    except Exception:
        return float(default)


def _int(name: str, default: int) -> int:
    try:
        return int(float(getattr(CFG, name, default)))
    except Exception:
        return int(default)


def lane_activation_decision(segment: dict[str, Any], *, lane: str, threshold: float | None) -> dict[str, Any]:
    rows = int(segment.get("rows") or 0)
    positives = int(segment.get("positives") or 0)
    unique_tokens = int(segment.get("unique_tokens") or rows)
    holdout_rows = int(segment.get("holdout_rows") or rows)
    holdout_positives = int(segment.get("holdout_positives") or positives)
    selected_total = float(segment.get("selected_total_pnl") or segment.get("selected_total_pnl_pct_points") or 0.0)
    baseline_total = float(segment.get("total_pnl_pct_points") or segment.get("baseline_total_pnl") or 0.0)
    jackpot_capture = segment.get("jackpot_capture_rate")
    jackpot_capture_f = 1.0 if jackpot_capture is None else float(jackpot_capture or 0.0)

    blockers: list[str] = []
    if rows < _int("ML_MIN_LANE_ROWS", 120):
        blockers.append("rows")
    if positives < _int("ML_MIN_LANE_POSITIVES", 25):
        blockers.append("positives")
    if unique_tokens < _int("ML_MIN_LANE_UNIQUE_TOKENS", 120):
        blockers.append("unique_tokens")
    if holdout_rows < _int("ML_MIN_LANE_HOLDOUT_ROWS", 30):
        blockers.append("holdout_rows")
    if holdout_positives < _int("ML_MIN_LANE_HOLDOUT_POSITIVES", 6):
        blockers.append("holdout_positives")
    max_degradation = _num("ML_MAX_SELECTED_PNL_DEGRADATION_PCT", 0.0)
    min_selected_total = baseline_total * (1.0 - max(0.0, max_degradation) / 100.0)
    if selected_total < min_selected_total:
        blockers.append("model_reduces_total_pnl")
    if jackpot_capture_f < _num("ML_MIN_JACKPOT_CAPTURE_RATE", 0.80):
        blockers.append("jackpot_capture")

    lane_norm = normalize_entry_lane(lane)
    activation_ready = not blockers
    if lane_norm in LIVE_PROFIT_LANES:
        mode = "enforce" if activation_ready else "sizing_only"
    elif lane_norm in RESEARCH_LANES:
        mode = "enforce" if activation_ready else "shadow"
    else:
        mode = "shadow"
        if "unknown_lane" not in blockers:
            blockers.append("unknown_lane")
        activation_ready = False

    return {
        "threshold": threshold,
        "activation_ready": bool(activation_ready),
        "mode_recommended": mode,
        "reason": "ok" if activation_ready else ",".join(blockers),
        "do_not_enforce": not activation_ready,
        "checks": {
            "rows": rows,
            "positives": positives,
            "unique_tokens": unique_tokens,
            "holdout_rows": holdout_rows,
            "holdout_positives": holdout_positives,
            "selected_total_pnl": selected_total,
            "baseline_total_pnl": baseline_total,
            "jackpot_capture_rate": jackpot_capture,
        },
    }


def build_recommended_thresholds_by_lane(segment_report: dict[str, Any], global_result: dict[str, Any] | None = None) -> dict[str, Any]:
    global_result = global_result or {}
    global_threshold = global_result.get("picked") or segment_report.get("threshold")
    global_activation_ready = bool(global_result.get("activation_ready", False))
    out: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "global": {
            "threshold": global_threshold,
            "activation_ready": global_activation_ready,
            "mode_recommended": "shadow" if not global_activation_ready else "enforce",
            "reason": global_result.get("activation_reason") or "global_threshold",
        },
        "by_lane": {},
    }
    segments = segment_report.get("segments") if isinstance(segment_report, dict) else {}
    lane_segments = segments.get("entry_lane") if isinstance(segments, dict) else {}
    if isinstance(lane_segments, dict):
        for lane, segment in lane_segments.items():
            if not isinstance(segment, dict):
                continue
            threshold = segment.get("picked_threshold", global_threshold)
            out["by_lane"][str(lane)] = lane_activation_decision(segment, lane=str(lane), threshold=threshold)
    return out


__all__ = ["lane_activation_decision", "build_recommended_thresholds_by_lane"]
