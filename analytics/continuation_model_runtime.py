from __future__ import annotations

from typing import Any

from analytics.model_runtime_common import predict_model, predict_regression_estimate
from analytics.inference_scope import ensure_inference_scope


def predict_continuation(vec: Any) -> dict[str, Any]:
    with ensure_inference_scope():
        return _predict_continuation(vec)


def _predict_continuation(vec: Any) -> dict[str, Any]:
    one_details = predict_regression_estimate("continuation", "continuation_peak_after_seen_1m", vec)
    three_details = predict_regression_estimate("continuation", "continuation_peak_after_seen_3m", vec)
    one, three = one_details["value"], three_details["value"]
    pos = predict_model("continuation", "continuation_positive_after_seen", vec)
    score = three if three is not None else one
    return {
        "continuation_1m": None if one is None else float(one),
        "continuation_3m": None if three is None else float(three),
        "continuation_positive_proba": None if pos is None else float(pos),
        "continuation_score": None if score is None else float(score),
        "continuation_1m_estimate": one_details,
        "continuation_3m_estimate": three_details,
    }


__all__ = ["predict_continuation"]
