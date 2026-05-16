from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.birth_probe_micro_canary import write_birth_probe_micro_canary_report


def main() -> int:
    report = write_birth_probe_micro_canary_report(ROOT)
    print(
        "birth_probe_micro_canary_report "
        f"groups={len(report.get('reason_groups', {}))} "
        f"recommended={len(report.get('recommended_groups', {}))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
