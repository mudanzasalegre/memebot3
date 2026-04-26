from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.audit import load_closed_positions_context


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize closed trade history without trading side effects.")
    parser.add_argument("--out", default="data/metrics/trade_history_audit.json")
    args = parser.parse_args()
    frame = load_closed_positions_context()
    summary = {
        "rows": int(len(frame)),
        "avg_pnl_pct": float(frame["computed_total_pnl_pct"].mean()) if not frame.empty else None,
        "median_pnl_pct": float(frame["computed_total_pnl_pct"].median()) if not frame.empty else None,
        "total_pnl_pct_points": float(frame["computed_total_pnl_pct"].sum()) if not frame.empty else 0.0,
        "max_pnl_pct": float(frame["computed_total_pnl_pct"].max()) if not frame.empty else None,
        "min_pnl_pct": float(frame["computed_total_pnl_pct"].min()) if not frame.empty else None,
    }
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
