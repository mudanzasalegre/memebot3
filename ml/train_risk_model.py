from __future__ import annotations

import json

from config.config import PROJECT_ROOT
from ml.family_training import train_classifier_family


def train_risk_models() -> dict:
    report = train_classifier_family(
        family="risk",
        targets=["severe_loss_30", "severe_loss_50"],
        feature_set_name="risk_features",
    )
    path = PROJECT_ROOT / "data" / "metrics" / "risk_model_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(train_risk_models(), indent=2))
