from __future__ import annotations

from typing import Any

from analytics.model_runtime_common import predict_model
from analytics.risk_predict import predict_risk


def predict_severe_loss_risk(vec: Any) -> dict[str, float | str | None]:
    risk30 = predict_model("risk", "severe_loss_30", vec)
    risk50 = predict_model("risk", "severe_loss_50", vec)
    if risk30 is None:
        risk30 = predict_risk(vec)
    level = "unknown"
    if risk50 is not None and float(risk50) >= 0.70:
        level = "lethal"
    elif risk30 is not None and float(risk30) >= 0.70:
        level = "high"
    elif risk30 is not None:
        level = "low" if float(risk30) < 0.35 else "medium"
    return {
        "risk_proba_30": None if risk30 is None else float(risk30),
        "risk_proba_50": None if risk50 is None else float(risk50),
        "risk_level": level,
        "risk_reason": "family_model" if risk30 is not None else "model_missing",
    }


__all__ = ["predict_severe_loss_risk"]
