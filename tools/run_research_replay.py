from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_loop.replay_runner import run_research_replay, run_research_replay_from_sandbox


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local AutoResearch replay for one candidate.")
    parser.add_argument("candidate", nargs="?", help="Candidate policy JSON path.")
    parser.add_argument("--candidate", dest="candidate_option", help="Candidate policy JSON path.")
    parser.add_argument("--run-dir", help="Existing data/research_runs/runs/<run_id> directory.")
    parser.add_argument("--run-id", help="Optional run id when creating a new sandbox.")
    parser.add_argument("--root", default=str(ROOT), help="Project root. Defaults to this checkout.")
    parser.add_argument("--no-regenerate", action="store_true", help="Use existing local reports only.")
    args = parser.parse_args()

    root = Path(args.root)
    candidate_path = args.candidate_option or args.candidate
    if args.run_dir:
        result = run_research_replay_from_sandbox(
            Path(args.run_dir),
            root=root,
            regenerate=not args.no_regenerate,
        )
    elif candidate_path:
        result = run_research_replay(
            Path(candidate_path),
            root=root,
            run_id=args.run_id,
            regenerate=not args.no_regenerate,
        )
    else:
        parser.error("candidate or --run-dir is required")
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
    return 0 if result.status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
