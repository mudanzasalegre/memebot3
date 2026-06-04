from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_loop.candidate_generator import GENERATION_MODES, generate_research_candidates, write_candidate_policies


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate AutoResearch candidate policies.")
    parser.add_argument("--space", required=True, help="Search space name, e.g. moonshot_micro.")
    parser.add_argument("--n", type=int, required=True, help="Number of candidates to generate.")
    parser.add_argument("--seed", type=int, default=None, help="Seed for seeded_random/local modes.")
    parser.add_argument("--mode", choices=sorted(GENERATION_MODES), default="seeded_random")
    parser.add_argument("--root", default=str(ROOT), help="Project root. Defaults to this checkout.")
    parser.add_argument("--no-write", action="store_true", help="Print only; do not write candidate JSON files.")
    args = parser.parse_args()

    candidates = generate_research_candidates(
        space_name=args.space,
        n=args.n,
        mode=args.mode,
        seed=args.seed,
        root=Path(args.root),
        write=False,
    )
    paths = [] if args.no_write else write_candidate_policies(candidates, root=Path(args.root))
    print(
        json.dumps(
            {
                "generated": len(candidates),
                "space": args.space,
                "mode": args.mode,
                "seed": args.seed,
                "paths": [str(path) for path in paths],
                "proposal_ids": [candidate["proposal_id"] for candidate in candidates],
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
