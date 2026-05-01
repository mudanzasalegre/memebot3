from __future__ import annotations

from typing import Any

from analytics.model_runtime_common import predict_model


def predict_runner_probabilities(vec: Any) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for threshold in (100, 300, 500):
        value = predict_model("runner", f"runner_{threshold}", vec)
        out[f"runner{threshold}_proba"] = None if value is None else float(value)
    return out


__all__ = ["predict_runner_probabilities"]
