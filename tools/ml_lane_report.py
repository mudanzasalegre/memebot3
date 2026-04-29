from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.lane_reports import write_ml_lane_report


def main() -> None:
    print(json.dumps(write_ml_lane_report(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
