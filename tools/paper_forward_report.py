from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.paper_forward_evaluator import write_candidate_policy_forward_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a candidate policy in paper-forward gate mode.")
    parser.add_argument("proposal", nargs="?", help="candidate proposal JSON path")
    args = parser.parse_args()
    if args.proposal:
        candidate = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
    else:
        candidate = {"proposal_id": "ad_hoc", "policy_name": "combined_policy_v2", "live_allowed": False}
    report = write_candidate_policy_forward_report(candidate)
    print(json.dumps({"passed": report.get("passed"), "proposal_id": report.get("proposal_id")}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
