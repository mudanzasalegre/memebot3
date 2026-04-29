from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.socials_report import SOCIALS_REPORT_JSON, SOCIALS_REPORT_MD, write_socials_report


def main() -> None:
    report = write_socials_report()
    print(json.dumps({
        "json": str(SOCIALS_REPORT_JSON),
        "markdown": str(SOCIALS_REPORT_MD),
        "social_rows": report.get("social_rows"),
        "coverage_pct": report.get("socials_coverage_pct"),
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()
