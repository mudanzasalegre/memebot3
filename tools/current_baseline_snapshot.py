from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.baseline_snapshot import write_current_baseline_snapshot


def main() -> int:
    snapshot = write_current_baseline_snapshot(ROOT)
    print(f"wrote current baseline: trades={snapshot['trades']['rows']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
