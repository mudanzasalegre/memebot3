from __future__ import annotations

import argparse
import datetime as dt
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
from research_loop.runtime_state import (
    EVENT_AUTORESEARCH_ERROR,
    EVENT_AUTORESEARCH_STOP,
    append_event,
    record_cycle_completion,
    record_cycle_start,
    record_runtime_error,
    record_runtime_start,
)


def _next_cycle_at(interval_hours: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=interval_hours)).isoformat()


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
    record_runtime_start(root, config, once=once, interval_hours=config.interval_hours)
    exit_code = 0
    try:
        while True:
            record_cycle_start(root, config, next_cycle_at_utc=None if once else _next_cycle_at(config.interval_hours))
            result = run_autoresearch_cycle(root=root, config=config, seed=args.seed)
            result_payload = result.as_dict()
            results.append(result_payload)
            record_cycle_completion(
                root,
                config,
                result,
                next_cycle_at_utc=None if once else _next_cycle_at(config.interval_hours),
            )
            print(json.dumps(result_payload, indent=2, sort_keys=True, default=str), flush=True)
            if once:
                exit_code = 0 if result.status != "failed" else 1
                break
            time.sleep(config.interval_hours * 3600.0)
    except Exception as exc:
        exit_code = 1
        record_runtime_error(root, str(exc))
        append_event(root, EVENT_AUTORESEARCH_ERROR, {"phase": "process", "error": str(exc)})
        raise
    finally:
        append_event(root, EVENT_AUTORESEARCH_STOP, {"exit_code": exit_code})
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
