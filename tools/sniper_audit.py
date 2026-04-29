from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from analytics.sniper_audit import write_sniper_audit


def main() -> None:
    report = write_sniper_audit()
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
