"""Exercise automatic runner learning on real records in an isolated directory."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.family_training import load_training_frame
from ml.runner_advisory_learning import train_runner_advisory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    frame = load_training_frame()
    isolated = ROOT / "data" / "validation" / ("runner-advisory-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    result = train_runner_advisory(root=isolated, frame=frame)
    result.update({"audit_artifact_root": str(isolated.relative_to(ROOT)).replace("\\", "/"),
                   "bot_started": False, "active_runtime_models_changed": False,
                   "profitability_established": False,
                   "target_join": frame.attrs.get("outcome_target_join"),
                   "source_decision_start": str(frame.timestamp.min()),
                   "source_decision_end": str(frame.timestamp.max())})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": result["status"], "isolated_ranking_update": result["updated"],
                      "selected_targets": [name for name, item in result["decisions"].items() if item["selected"]],
                      "bot_started": False, "active_runtime_models_changed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
