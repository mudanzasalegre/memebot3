from __future__ import annotations

from typing import Any

from analytics.ev_predict import predict_ev
from analytics.model_runtime_common import predict_model, predict_regression_estimate


def predict_ev_scores(vec: Any) -> dict[str, Any]:
    estimate = predict_regression_estimate("ev", "ev_realized_clipped", vec)
    pred = estimate["value"]
    if pred is None:
        pred = predict_ev(vec)
    peak = predict_model("ev", "ev_peak_adjusted", vec)
    return {
        "ev_pred_pct": None if pred is None else float(pred),
        "ev_peak_adjusted_pred_pct": None if peak is None else float(peak),
        # A large prediction is not high confidence. No conditional confidence
        # level has been estimated, so do not fabricate one from its magnitude.
        "ev_confidence": None,
        "ev_estimate": estimate if estimate["value"] is not None else {**estimate, "value": pred, "status": "compatibility_estimate_without_interval" if pred is not None else "unknown"},
    }


__all__ = ["predict_ev_scores"]
