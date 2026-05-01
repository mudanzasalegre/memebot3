from __future__ import annotations

from typing import Any

from analytics.model_runtime_common import predict_model


def predict_continuation(vec: Any) -> dict[str, float | None]:
    one = predict_model("continuation", "continuation_peak_after_seen_1m", vec)
    three = predict_model("continuation", "continuation_peak_after_seen_3m", vec)
    pos = predict_model("continuation", "continuation_positive_after_seen", vec)
    score = three if three is not None else one
    return {
        "continuation_1m": None if one is None else float(one),
        "continuation_3m": None if three is None else float(three),
        "continuation_positive_proba": None if pos is None else float(pos),
        "continuation_score": None if score is None else float(score),
    }


__all__ = ["predict_continuation"]
