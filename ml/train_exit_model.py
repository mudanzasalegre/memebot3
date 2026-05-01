from __future__ import annotations

import json

from config.config import PROJECT_ROOT
from ml.family_training import train_exit_classifier


def train_exit_model() -> dict:
    report = train_exit_classifier()
    path = PROJECT_ROOT / "data" / "metrics" / "exit_model_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(train_exit_model(), indent=2))
