from __future__ import annotations

from typing import Any

from analytics.ai_predict import model_runtime_status, threshold_runtime_metadata
from analytics.ml_policy import MlPolicyDecision, decide_ml_action


def ml_runtime_snapshot() -> dict[str, Any]:
    return {
        "model": model_runtime_status(),
        "thresholds": threshold_runtime_metadata(),
    }


__all__ = [
    "MlPolicyDecision",
    "decide_ml_action",
    "ml_runtime_snapshot",
    "model_runtime_status",
    "threshold_runtime_metadata",
]
