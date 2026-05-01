from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.paper_forward import evaluate_paper_forward
from analytics.strategy_proposal_validator import load_and_validate
from backtest.policy_replay import build_policy_replay


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and replay a strategy proposal without applying it.")
    parser.add_argument("proposal", type=Path)
    args = parser.parse_args()
    validation = load_and_validate(args.proposal)
    replay = build_policy_replay(ROOT)
    paper = evaluate_paper_forward(validation["proposal"], root=ROOT) if validation["ok"] else {"passed": False, "reason": "invalid_proposal"}
    report = {"validation": validation, "policy_replay": replay, "paper_forward": paper}
    out = ROOT / "data" / "metrics" / "strategy_proposal_review.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0 if validation["ok"] and paper.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
