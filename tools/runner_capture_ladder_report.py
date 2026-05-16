from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.runner_capture_ladder_report import write_runner_capture_ladder_report


def main() -> int:
    report = write_runner_capture_ladder_report(ROOT)
    summary = report.get("summary", {})
    print(
        "runner_capture_ladder_report "
        f"rows={summary.get('rows', 0)} "
        f"avg_sim_capture={summary.get('avg_simulated_capture_ratio', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
