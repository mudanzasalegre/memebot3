"""Offline, reproducible runner-learning audit; never starts or promotes a bot."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from ml.family_training import load_training_frame, train_classifier_family, train_regressor_family
from ml.label_builder import RUNNER_THRESHOLDS


def audit_runner_learning(root: Path = ROOT, *, output_dir: Path | None = None) -> dict:
    frame = load_training_frame()
    output_dir = output_dir or root / "data" / "validation" / f"runner-learning-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    classifiers = train_classifier_family(
        family="runner", targets=[f"runner_{threshold}" for threshold in RUNNER_THRESHOLDS],
        feature_set_name="runner_features", frame=frame, output_dir=output_dir / "runner",
    )
    regression = train_regressor_family(
        family="ev", targets=["ev_realized_clipped"], feature_set_name="ev_features",
        frame=frame, output_dir=output_dir / "ev",
    )
    sources = []
    for path in [*(root / "data" / "features").glob("features_*.parquet"), root / "data" / "metrics" / "candidate_outcomes.jsonl"]:
        if path.exists():
            sources.append({"file": path.relative_to(root).as_posix(), "sha256": sha256(path.read_bytes()).hexdigest(),
                            "mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})
    # Artifact paths are audit-local, not activated family-model paths.
    for report in (classifiers, regression):
        for target in report.get("targets", {}).values():
            if target.get("model_path"):
                target["model_path"] = Path(target["model_path"]).relative_to(root).as_posix()
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "sources": sources,
        "rows": len(frame), "target_join": frame.attrs.get("outcome_target_join"),
        "decision_period_utc": {"start": str(frame.timestamp.min()), "end": str(frame.timestamp.max())},
        "target_counts": {f"runner_{t}": {"observed": int(frame[f"runner_{t}"].notna().sum()),
                                          "positive": int(frame[f"runner_{t}"].sum())} for t in RUNNER_THRESHOLDS},
        "classification": classifiers, "realized_return_regression": regression,
        "promotion_attempted": False, "bot_started": False,
        "caveats": ["Runner targets are observed price peaks, not executable or costed profits.",
                    "Historical shadow observations do not establish future profitability.",
                    "Rare extreme targets can lack enough positive observations; no positive class is invented.",
                    "Learning audit artifacts are isolated from runtime models and no live model is activated."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_runner_learning()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"report": str(args.output), "rows": report["rows"], "target_join": report["target_join"],
                      "promotion_attempted": False, "bot_started": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
