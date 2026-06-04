from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_loop.candidate_generator import GENERATION_MODES
from research_loop.batch_runner import run_research_batch


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate, replay, evaluate and checkpoint an AutoResearch batch.")
    parser.add_argument("--space", required=True, help="Search space name, or 'auto'/'bandit'.")
    parser.add_argument("--n", type=int, required=True, help="Number of candidates.")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--mode", choices=sorted(GENERATION_MODES), default="seeded_random")
    parser.add_argument("--root", default=str(ROOT), help="Project root. Defaults to this checkout.")
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--regenerate-baseline", action="store_true")
    parser.add_argument("--regenerate-replay", action="store_true")
    parser.add_argument("--min-closed-trades", type=int, default=0)
    args = parser.parse_args()

    result = run_research_batch(
        space_name=args.space,
        n=args.n,
        seed=args.seed,
        mode=args.mode,
        root=Path(args.root),
        batch_id=args.batch_id,
        regenerate_baseline=args.regenerate_baseline,
        regenerate_replay=args.regenerate_replay,
        min_closed_trades=args.min_closed_trades,
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
