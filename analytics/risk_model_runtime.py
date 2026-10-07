from __future__ import annotations

from typing import Any

from analytics.model_runtime_common import predict_model
from analytics.risk_predict import predict_risk


def predict_severe_loss_risk(vec: Any) -> dict[str, float | str | None]:
    risk30 = predict_model("risk", "severe_loss_30", vec)
    risk50 = predict_model("risk", "severe_loss_50", vec)
    crush = predict_model("risk", "liquidity_crush_loss", vec)
    toxic = predict_model("risk", "toxic_exit_loss", vec)
    family_model_seen = any(value is not None for value in (risk30, risk50, crush, toxic))
    if risk30 is None:
        risk30 = predict_risk(vec)
    max_known = max(
        [float(value) for value in (risk30, risk50, crush, toxic) if value is not None],
        default=None,
    )
    level = "unknown"
    if risk50 is not None and float(risk50) >= 0.70:
        level = "lethal"
    elif max_known is not None and float(max_known) >= 0.70:
        level = "high"
    elif max_known is not None:
        level = "low" if float(max_known) < 0.35 else "medium"
    return {
        "risk_proba_30": None if risk30 is None else float(risk30),
        "risk_proba_50": None if risk50 is None else float(risk50),
        "risk_proba_liquidity_crush": None if crush is None else float(crush),
        "risk_proba_toxic_exit": None if toxic is None else float(toxic),
        "risk_level": level,
        "risk_reason": "family_model" if family_model_seen else ("legacy_model" if risk30 is not None else "model_missing"),
    }


__all__ = ["predict_severe_loss_risk"]
