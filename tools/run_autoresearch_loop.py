from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_loop.scheduler import (
    evaluate_paper_profitability_for_demotion,
    load_scheduler_config,
    run_autoresearch_cycle,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AutoResearch continuous loop once or as a daemon.")
    parser.add_argument("--root", default=str(ROOT), help="Project root. Defaults to this checkout.")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit. This is the default unless --daemon is set.")
    parser.add_argument("--daemon", action="store_true", help="Run continuously using AUTORESEARCH_INTERVAL_HOURS.")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--space", default=None, help="Override bandit/idle space selection.")
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--max-parallel", type=int, default=None)
    parser.add_argument("--mode", default=None, help="Generation mode, e.g. seeded_random or grid.")
    parser.add_argument("--idle-threshold-hours", type=float, default=None)
    parser.add_argument("--interval-hours", type=float, default=None)
    parser.add_argument("--regenerate-reports", action="store_true")
    parser.add_argument("--no-paper-promote", action="store_true")
    parser.add_argument("--no-demotion", action="store_true")
    parser.add_argument("--demotion-only", action="store_true", help="Only evaluate profitability-aware demotion.")
    args = parser.parse_args()

    overrides = {
        key: value
        for key, value in {
            "space": args.space,
            "max_candidates_per_cycle": args.max_candidates,
            "max_parallel": args.max_parallel,
            "batch_mode": args.mode,
            "idle_threshold_hours": args.idle_threshold_hours,
            "interval_hours": args.interval_hours,
            "regenerate_reports": True if args.regenerate_reports else None,
            "auto_paper_promote": False if args.no_paper_promote else None,
            "profitability_demotion_enabled": False if args.no_demotion else None,
        }.items()
        if value is not None
    }
    config = load_scheduler_config(overrides=overrides)
    root = Path(args.root)

    if args.demotion_only:
        result = evaluate_paper_profitability_for_demotion(root=root)
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
        return 0 if result.status != "missing_paper_state" else 1

    once = args.once or not args.daemon
    results = []
    while True:
        result = run_autoresearch_cycle(root=root, config=config, seed=args.seed)
        results.append(result.as_dict())
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
        if once:
            return 0 if result.status != "failed" else 1
        time.sleep(config.interval_hours * 3600.0)


if __name__ == "__main__":
    raise SystemExit(main())
