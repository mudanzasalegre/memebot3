from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.runner_jackpot_report import write_runner_jackpot_report


def main() -> int:
    report = write_runner_jackpot_report()
    print(json.dumps(report["runners"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
