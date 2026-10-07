from __future__ import annotations

import math
from typing import Any

from config.config import CFG, PROJECT_ROOT
from analytics.model_runtime_common import invalidate_model_cache, predict_artifact

MODEL_PATH = PROJECT_ROOT / "ml" / "ev_model.pkl"
META_PATH = PROJECT_ROOT / "ml" / "ev_model.meta.json"


def predict_ev(vec: Any) -> float | None:
    """Only validated configured-clipped return estimates; not dollars or confidence."""
    try:
        low = float(getattr(CFG, "ML_EV_CLIP_MIN", -100.0))
        high = float(getattr(CFG, "ML_EV_CLIP_MAX", 300.0))
        if not math.isfinite(low) or not math.isfinite(high) or low >= high:
            return None
        prediction = predict_artifact(MODEL_PATH, vec, expected_metadata={
            "family": "ev", "target": "ev_configured_clipped", "clip_min": low, "clip_max": high})
        return None if prediction is None else float(prediction)
    except (TypeError, ValueError, OverflowError):
        return None


def reload_ev_model() -> None:
    invalidate_model_cache(MODEL_PATH)


__all__ = ["predict_ev", "reload_ev_model"]
