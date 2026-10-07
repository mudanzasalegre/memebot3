from __future__ import annotations

from itertools import product
from pathlib import Path
from typing import Any

from config.config import PROJECT_ROOT
from analytics.report_utils import write_json


def generate_candidate_profiles(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or PROJECT_ROOT
    candidates = []
    for risk_max, ev_min, runner_min in product((0.35, 0.50, 0.70), (0.0, 10.0, 25.0), (0.10, 0.20)):
        # These are hypotheses, not evaluated policies. A threshold is not a
        # financial return and the incumbent replay cannot score a challenger.
        candidates.append(
            {
                "proposal_id": f"offline_r{risk_max}_e{ev_min}_u{runner_min}".replace(".", "_"),
                "thresholds": {"risk_max": risk_max, "ev_min": ev_min, "runner_min": runner_min},
                "evaluation_status": "not_evaluated",
                "expected_metrics": {"score": None, "source": None},
                "ranking_basis": "deterministic_hypothesis_order_not_performance",
                "required_gates": ["policy_replay", "paper_forward", "manual_approval"],
                "live_allowed": False,
            }
        )
    return candidates


def write_candidate_profiles(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or PROJECT_ROOT
    target_dir = root / "strategy_proposals" / "candidates"
    target_dir.mkdir(parents=True, exist_ok=True)
    candidates = generate_candidate_profiles(root)
    for candidate in candidates[:10]:
        write_json(target_dir / f"{candidate['proposal_id']}.json", candidate)
    return candidates


__all__ = ["generate_candidate_profiles", "write_candidate_profiles"]
