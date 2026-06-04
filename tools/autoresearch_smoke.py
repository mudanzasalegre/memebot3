from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_loop.candidate_generator import GENERATION_MODES
from research_loop.smoke import run_autoresearch_smoke


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AutoResearch end-to-end local smoke.")
    parser.add_argument("--root", default=str(ROOT), help="Project root. Defaults to this checkout.")
    parser.add_argument("--space", default="moonshot_micro", help="Search space used to generate smoke candidates.")
    parser.add_argument("--n", type=int, default=3, help="Number of candidates to generate.")
    parser.add_argument("--seed", type=int, default=33)
    parser.add_argument("--mode", choices=sorted(GENERATION_MODES), default="seeded_random")
    parser.add_argument("--smoke-id", default=None)
    parser.add_argument(
        "--overwrite-fixture-metrics",
        action="store_true",
        help="Overwrite local metric fixtures before replay. Default only fills missing files.",
    )
    parser.add_argument(
        "--regenerate-replay-reports",
        action="store_true",
        help="Run the normal report regeneration hook before each replay.",
    )
    args = parser.parse_args()

    result = run_autoresearch_smoke(
        root=Path(args.root),
        space_name=args.space,
        n=args.n,
        seed=args.seed,
        mode=args.mode,
        smoke_id=args.smoke_id,
        overwrite_fixture_metrics=args.overwrite_fixture_metrics,
        regenerate_replay_reports=args.regenerate_replay_reports,
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
    print("autoresearch_smoke=ok" if result.status == "ok" else "autoresearch_smoke=fail")
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
