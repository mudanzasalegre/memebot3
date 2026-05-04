from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.post_run_baseline import BASELINE_DOC, BASELINE_JSON, write_post_run_baseline


def main() -> int:
    report = write_post_run_baseline(ROOT)
    summary = {
        "closed_trades": report["global"]["count"],
        "win_rate_pct": report["global"]["win_rate_pct"],
        "avg_pnl_pct": report["global"]["avg_pnl_pct"],
        "severe_loss_count": report["global"]["severe_loss_count"],
        "json": str(ROOT / BASELINE_JSON),
        "markdown": str(ROOT / BASELINE_DOC),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
