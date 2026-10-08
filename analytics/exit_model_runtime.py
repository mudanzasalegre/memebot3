from __future__ import annotations

from typing import Any

from analytics.model_runtime_common import predict_diagnostic_exit_label, family_model_selection
from analytics.inference_scope import ensure_inference_scope


def predict_exit_profile(vec: Any) -> dict[str, Any]:
    with ensure_inference_scope():
        profile = predict_diagnostic_exit_label(vec)
        selection = family_model_selection("exit", targets=["best_exit_profile"])
    return {"exit_profile": profile, "exit_reason": "diagnostic_exit_style" if profile else "model_missing_or_unvalidated",
        "status": "validated_historical_diagnostic" if profile else "unknown",
        "activation_role": "diagnostic_only", "exit_policy_permission": False, "buy_permission": False,
        "scope": "observed_outcome_style_not_optimal_costed_exit", "model_selection": selection}


__all__ = ["predict_exit_profile"]
