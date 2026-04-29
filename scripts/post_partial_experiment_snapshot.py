from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.post_partial_experiment import refresh_snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the paper-shadow post-partial experiment snapshot.")
    parser.add_argument(
        "--reset-start",
        action="store_true",
        help="Reset the experiment baseline to the current paper portfolio before writing the snapshot.",
    )
    args = parser.parse_args()
    snapshot = refresh_snapshot(force_reset=args.reset_start)
    print(json.dumps(snapshot, indent=2))


if __name__ == "__main__":
    main()
