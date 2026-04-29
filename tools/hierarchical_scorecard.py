from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.scorecard import write_hierarchical_scorecard


def main() -> None:
    report = write_hierarchical_scorecard()
    print(json.dumps({"groups": len(report)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
