from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_loop.scoreboard import load_scoreboard, record_run_evaluation, write_scoreboard


def main() -> int:
    parser = argparse.ArgumentParser(description="Read or update the AutoResearch scoreboard.")
    parser.add_argument("--root", default=str(ROOT), help="Project root. Defaults to this checkout.")
    parser.add_argument("--run-dir", help="Replay run directory to evaluate and record.")
    parser.add_argument("--baseline-metrics", help="Baseline metrics JSON path for --run-dir.")
    parser.add_argument("--min-closed-trades", type=int, default=0)
    parser.add_argument("--rewrite", action="store_true", help="Rewrite scoreboard.md from existing JSON.")
    args = parser.parse_args()

    root = Path(args.root)
    if args.run_dir:
        entry = record_run_evaluation(
            Path(args.run_dir),
            Path(args.baseline_metrics) if args.baseline_metrics else None,
            root=root,
            min_closed_trades=args.min_closed_trades,
        )
        print(json.dumps({"scoreboard": "updated", "entry": entry}, indent=2, sort_keys=True, default=str))
        return 0

    entries = load_scoreboard(root)
    if args.rewrite:
        write_scoreboard(entries, root=root)
    print(json.dumps({"entries": len(entries), "scoreboard": entries}, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
