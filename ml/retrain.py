"""Report a pre-approved atomic promotion; never compare unrelated run scores."""
from __future__ import annotations

import logging
import math

from utils.venv_bootstrap import ensure_project_venv

ensure_project_venv(__file__, module_name=__spec__.name if __spec__ else None)

from ml.train import TRAIN_STATUS_JSON, TrainResult, train_and_save

log = logging.getLogger("ml.retrain")


def _selection(metric_meta: dict) -> tuple[str | None, float | None]:
    """Legacy diagnostic formatting only: never an activation criterion."""
    metric, score = metric_meta.get("model_selection_metric"), metric_meta.get("model_selection_score")
    if isinstance(metric, str) and type(score) in (int, float) and math.isfinite(score):
        return metric, float(score)
    score = metric_meta.get("auc_pr_forward_or_cv_mean")
    if type(score) in (int, float) and math.isfinite(score):
        return "auc_pr_forward_or_cv_mean", float(score)
    return None, None


def retrain_if_better() -> bool:
    # Training reserves a later same-target cohort before model selection,
    # compares both models before activation, and commits one immutable bundle.
    # Restoring old aliases here would desynchronize the authoritative selector.
    result: TrainResult = train_and_save()
    promoted = result.trained is True and getattr(result, "active_promoted", False) is True
    if promoted:
        log.info("Primary model selected after checked same-later-cohort approval")
        if getattr(result, "publication_warnings", None):
            log.warning("Primary selector committed; diagnostic publication warnings: %s", result.publication_warnings)
    else:
        log.info("Primary promotion not applied: %s (see %s)", result.status, TRAIN_STATUS_JSON)
    return promoted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    retrain_if_better()
