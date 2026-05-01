from __future__ import annotations

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.family_training import load_training_frame
from ml.walk_forward import walk_forward_report


def main() -> int:
    frame = load_training_frame()
    report = walk_forward_report(frame)
    path = ROOT / "data" / "metrics" / "walk_forward_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
