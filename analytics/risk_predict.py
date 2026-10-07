from __future__ import annotations

import math
from typing import Any

from config.config import CFG, PROJECT_ROOT
from analytics.model_runtime_common import invalidate_model_cache, predict_artifact, predict_model

MODEL_PATH = PROJECT_ROOT / "ml" / "risk_model.pkl"
META_PATH = PROJECT_ROOT / "ml" / "risk_model.meta.json"


def predict_risk(vec: Any, *, severe_loss_pct: float | None = None) -> float | None:
    """Preserve the API, not the old in-sample probability bypass."""
    try:
        threshold = float(getattr(CFG, "ML_SEVERE_LOSS_PCT", -30.0) if severe_loss_pct is None else severe_loss_pct)
        if not math.isfinite(threshold) or not -100 <= threshold < 0:
            return None
        if threshold == -30:
            probability = predict_model("risk", "severe_loss_30", vec)
            if probability is not None:
                return float(probability)
        probability = predict_artifact(MODEL_PATH, vec, expected_metadata={
            "family": "risk", "target": "severe_loss_configured", "severe_loss_pct": threshold})
        return None if probability is None else float(probability)
    except (TypeError, ValueError, OverflowError):
        return None


def reload_risk_model() -> None:
    invalidate_model_cache(MODEL_PATH)
    invalidate_model_cache(PROJECT_ROOT / "ml" / "models" / "risk" / "severe_loss_30.pkl")


__all__ = ["predict_risk", "reload_risk_model"]
