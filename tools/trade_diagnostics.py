from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.trade_diagnostics import write_trade_diagnostics_report


def main() -> None:
    report = write_trade_diagnostics_report()
    print(json.dumps(report.get("summary", {}), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
