from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.policies import selected_mask
from backtest.report import render_report, summarize_replay
from config.config import CFG, PROJECT_ROOT

VAL_PREDS = PROJECT_ROOT / "data" / "metrics" / "val_preds.csv"
OUT_JSON = PROJECT_ROOT / "data" / "metrics" / "backtest_report.json"
OUT_MD = PROJECT_ROOT / "data" / "metrics" / "backtest_report.md"


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay historical ML policies.")
    parser.add_argument("--policy", default="rules_only", choices=["rules_only", "ml_shadow", "lane_aware", "global_enforce", "risk_veto", "ev_sizing"])
    parser.add_argument("--csv", default=str(VAL_PREDS))
    parser.add_argument("--threshold", type=float, default=float(getattr(CFG, "AI_THRESHOLD", 0.5) or 0.5))
    parser.add_argument("--no-fail-if-no-data", action="store_true")
    args = parser.parse_args()
    path = Path(args.csv)
    if not path.exists():
        if args.no_fail_if_no_data:
            print("backtest=no_data")
            return 0
        raise SystemExit(f"No existe {path}")
    frame = pd.read_csv(path)
    selected = selected_mask(frame, args.policy, args.threshold)
    report = summarize_replay(frame, selected)
    report["policy"] = args.policy
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    OUT_MD.write_text(render_report(report), encoding="utf-8")
    print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
