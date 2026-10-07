from __future__ import annotations

import json

from config.config import PROJECT_ROOT
from ml.family_training import train_classifier_family
from ml.label_builder import RUNNER_THRESHOLDS


def train_runner_models() -> dict:
    report = train_classifier_family(
        family="runner",
        targets=[
            *(f"runner_{threshold}" for threshold in RUNNER_THRESHOLDS),
            "executable_moonshot_peak100",
            "executable_moonshot_peak500",
        ],
        feature_set_name="runner_features",
    )
    path = PROJECT_ROOT / "data" / "metrics" / "runner_model_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(train_runner_models(), indent=2))
