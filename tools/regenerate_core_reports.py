from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.core_report_scheduler import regenerate_core_reports


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate core strategy reports.")
    parser.add_argument("--include-test-events", action="store_true", help="Include smoke/test events in reports.")
    args = parser.parse_args()
    summary = regenerate_core_reports(ROOT, include_test_events=bool(args.include_test_events))
    warnings = summary.get("warnings") if isinstance(summary, dict) else {}
    print(
        json.dumps(
            {
                "generated_at_utc": summary.get("generated_at_utc"),
                "reports": sorted((summary.get("reports") or {}).keys()),
                "warnings": warnings or {},
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    if warnings:
        print("regenerate_core_reports=warn", file=sys.stderr)
    else:
        print("regenerate_core_reports=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
