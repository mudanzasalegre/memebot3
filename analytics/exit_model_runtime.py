from __future__ import annotations

from typing import Any

from analytics.model_runtime_common import predict_model


def predict_exit_profile(vec: Any) -> dict[str, str | None]:
    pred = predict_model("exit", "best_exit_profile", vec)
    profile = str(pred) if pred is not None else None
    if profile not in {None, "defensive", "balanced", "runner", "moonbag", "post_partial_protected", "bird_runner"}:
        profile = "balanced"
    return {"exit_profile": profile, "exit_reason": "exit_model" if profile else "model_missing"}


__all__ = ["predict_exit_profile"]
