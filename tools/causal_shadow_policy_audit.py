from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.causal_shadow_policy import write_causal_shadow_policy_audit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a causal shadow-outcome audit for one MemeBot3 run.")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    report = write_causal_shadow_policy_audit(ROOT, run_id=str(args.run_id))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
