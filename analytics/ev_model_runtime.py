from __future__ import annotations

from typing import Any

from analytics.ev_predict import predict_ev
from analytics.model_runtime_common import predict_model


def predict_ev_scores(vec: Any) -> dict[str, float | None]:
    pred = predict_model("ev", "ev_realized_clipped", vec)
    if pred is None:
        pred = predict_ev(vec)
    peak = predict_model("ev", "ev_peak_adjusted", vec)
    confidence = None if pred is None else max(0.0, min(1.0, abs(float(pred)) / 100.0))
    return {
        "ev_pred_pct": None if pred is None else float(pred),
        "ev_peak_adjusted_pred_pct": None if peak is None else float(peak),
        "ev_confidence": confidence,
    }


__all__ = ["predict_ev_scores"]
