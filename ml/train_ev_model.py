from __future__ import annotations

import json

from config.config import PROJECT_ROOT
from ml.family_training import train_regressor_family


def train_ev_models() -> dict:
    report = train_regressor_family(
        family="ev",
        targets=["ev_realized_clipped", "ev_peak_adjusted"],
        feature_set_name="green_sniper_features",
    )
    path = PROJECT_ROOT / "data" / "metrics" / "ev_model_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(train_ev_models(), indent=2))
