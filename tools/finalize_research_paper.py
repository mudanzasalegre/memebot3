from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_loop.paper_forward import finalize_paper_forward


def _read_json(path: str | None) -> dict | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Finalize an AutoResearch paper-forward run.")
    parser.add_argument("run", help="Paper-forward run id or data/research_runs/paper_forward/<run_id> path.")
    parser.add_argument("--root", default=str(ROOT), help="Project root. Defaults to this checkout.")
    parser.add_argument("--paper-metrics", default=None, help="Optional paper metrics JSON path.")
    parser.add_argument("--baseline-metrics", default=None, help="Optional baseline metrics JSON path.")
    parser.add_argument("--api-budget", default=None, help="Optional candidate API budget JSON path.")
    parser.add_argument("--baseline-api-budget", default=None, help="Optional baseline API budget JSON path.")
    parser.add_argument("--no-rollback", action="store_true")
    args = parser.parse_args()

    result = finalize_paper_forward(
        args.run,
        root=Path(args.root),
        paper_metrics=_read_json(args.paper_metrics),
        baseline_metrics=_read_json(args.baseline_metrics),
        api_budget=_read_json(args.api_budget),
        baseline_api_budget=_read_json(args.baseline_api_budget),
        rollback_on_reject=not args.no_rollback,
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
