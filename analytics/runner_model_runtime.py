from __future__ import annotations

from typing import Any

from analytics.model_runtime_common import predict_model
from analytics.inference_scope import ensure_inference_scope
from ml.label_builder import RUNNER_THRESHOLDS


def predict_runner_probabilities(vec: Any) -> dict[str, float | None]:
    with ensure_inference_scope():
        return _predict_runner_probabilities(vec)


def _predict_runner_probabilities(vec: Any) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    previous = 1.0
    for threshold in RUNNER_THRESHOLDS:
        value = predict_model("runner", f"runner_{threshold}", vec)
        # Nested events must not suggest P(10,000%) > P(100%). This is a
        # conservative projection, not probability calibration or an EV claim.
        probability = None if value is None else min(previous, max(0.0, min(1.0, float(value))))
        out[f"runner{threshold}_proba"] = probability
        if probability is not None:
            previous = probability
    return out


__all__ = ["predict_runner_probabilities"]
