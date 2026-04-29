from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.runner_capture import write_runner_capture_report


def main() -> None:
    print(json.dumps(write_runner_capture_report()["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
