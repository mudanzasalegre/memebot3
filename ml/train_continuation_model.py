from __future__ import annotations

import json

from config.config import PROJECT_ROOT
from ml.family_training import train_classifier_family, train_regressor_family


def train_continuation_models() -> dict:
    reg = train_regressor_family(
        family="continuation",
        targets=["continuation_peak_after_seen_1m", "continuation_peak_after_seen_3m", "continuation_drawdown_after_seen"],
        feature_set_name="late_momentum_features",
    )
    clf = train_classifier_family(
        family="continuation",
        targets=["continuation_positive_after_seen"],
        feature_set_name="late_momentum_features",
    )
    report = {"regression": reg, "classification": clf}
    path = PROJECT_ROOT / "data" / "metrics" / "continuation_model_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(train_continuation_models(), indent=2))
