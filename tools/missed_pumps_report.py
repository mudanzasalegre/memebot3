from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from analytics.missed_pumps import write_missed_pumps_report


def main() -> None:
    rows = write_missed_pumps_report()
    print(json.dumps({"missed_pumps": len(rows), "top": rows[:10]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
