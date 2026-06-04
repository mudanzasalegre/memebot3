from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_loop.api_budget import build_api_budget_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the local AutoResearch API budget report.")
    parser.add_argument("--root", default=str(ROOT), help="Project root. Defaults to this checkout.")
    parser.add_argument("--no-write", action="store_true", help="Print only; do not write report files.")
    args = parser.parse_args()
    report = build_api_budget_report(Path(args.root), write=not args.no_write)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
