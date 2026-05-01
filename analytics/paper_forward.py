from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backtest.policy_replay import build_policy_replay
from config.config import PROJECT_ROOT


def evaluate_paper_forward(candidate_policy: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    replay = build_policy_replay(root)
    current = replay.get("current") or {}
    combined = replay.get("combined_policy_v2") or replay.get("combined_v1") or {}
    passed = (
        float(combined.get("total_pnl") or 0.0) >= float(current.get("total_pnl") or 0.0)
        and int(combined.get("severe_loss_count") or 0) <= int(current.get("severe_loss_count") or 0)
    )
    return {
        "proposal_id": candidate_policy.get("proposal_id"),
        "passed": bool(passed),
        "baseline": current,
        "candidate": combined,
        "required_before_live": ["paper_forward_window", "manual_approval"],
    }


def write_paper_forward_report(candidate_policy: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = evaluate_paper_forward(candidate_policy, root=root)
    path = root / "data" / "metrics" / "paper_forward_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


__all__ = ["evaluate_paper_forward", "write_paper_forward_report"]
