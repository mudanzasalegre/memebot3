from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_loop.paper_forward import start_paper_forward
from research_loop.policy_promoter import DEFAULT_SOURCE_PROFILE


def _read_json(path: str | None) -> dict | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote an accepted replay candidate to a paper-forward profile.")
    parser.add_argument("candidate", help="Candidate policy JSON path.")
    parser.add_argument("--root", default=str(ROOT), help="Project root. Defaults to this checkout.")
    parser.add_argument("--run-id", default=None, help="Paper-forward run id.")
    parser.add_argument("--profile-id", default=None, help="Suffix for config/profiles/paper_research_candidate_<id>.env.")
    parser.add_argument("--source-profile", default=DEFAULT_SOURCE_PROFILE)
    parser.add_argument("--evaluation-status", default="accepted_replay")
    parser.add_argument("--budget-json", default=None, help="Optional JSON file with paper budget overrides.")
    parser.add_argument("--allow-needs-paper", action="store_true")
    args = parser.parse_args()

    result = start_paper_forward(
        Path(args.candidate),
        root=Path(args.root),
        run_id=args.run_id,
        evaluation_result=args.evaluation_status,
        budget=_read_json(args.budget_json),
        source_profile=args.source_profile,
        profile_id=args.profile_id,
        allow_needs_paper=args.allow_needs_paper,
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
