from __future__ import annotations

import json

from config.config import PROJECT_ROOT
from ml.family_training import train_classifier_family


def train_runner_models() -> dict:
    report = train_classifier_family(
        family="runner",
        targets=["runner_100", "runner_300", "runner_500"],
        feature_set_name="green_sniper_features",
    )
    path = PROJECT_ROOT / "data" / "metrics" / "runner_model_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(train_runner_models(), indent=2))
