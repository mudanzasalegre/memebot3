from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backtest.policy_replay import build_policy_replay
from config.config import PROJECT_ROOT
from analytics.forward_evidence import collect_forward_evidence, forward_acceptance


def evaluate_paper_forward(candidate_policy: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    replay = build_policy_replay(root)
    current = replay.get("current") or {}
    policy_name = str(candidate_policy.get("policy_name") or candidate_policy.get("replay_policy") or "combined_policy_v2")
    combined = replay.get(policy_name) or replay.get("combined_policy_v2") or replay.get("combined_v1") or {}
    replay_passed = bool(combined) and bool(current) and (
        float(combined.get("total_pnl") or 0.0) >= float(current.get("total_pnl") or 0.0)
        and int(combined.get("severe_loss_count") or 0) <= int(current.get("severe_loss_count") or 0)
        and float(combined.get("runner_capture_ratio") or 0.0) >= float(current.get("runner_capture_ratio") or 0.0)
    )
    evidence = collect_forward_evidence(root, run_id=candidate_policy.get("forward_run_id"),
                                        started_at=candidate_policy.get("forward_started_at"),
                                        config_hash=candidate_policy.get("config_hash"),
                                        profile=candidate_policy.get("config_profile"))
    acceptance = forward_acceptance(evidence)
    return {
        "proposal_id": candidate_policy.get("proposal_id"),
        "policy_name": policy_name,
        "passed": bool(replay_passed and acceptance["passed"]),
        "replay_passed": replay_passed,
        "forward_evidence": evidence,
        "forward_acceptance": acceptance,
        "baseline": current,
        "candidate": combined,
        "required_before_live": ["policy_replay", "paper_forward_window", "manual_approval"],
    }


def write_paper_forward_report(candidate_policy: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = evaluate_paper_forward(candidate_policy, root=root)
    path = root / "data" / "metrics" / "paper_forward_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


__all__ = ["evaluate_paper_forward", "write_paper_forward_report"]
