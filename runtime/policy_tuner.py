from __future__ import annotations

from itertools import product
from pathlib import Path
from typing import Any

from backtest.policy_replay import build_policy_replay
from config.config import PROJECT_ROOT
from analytics.report_utils import write_json


def generate_candidate_profiles(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or PROJECT_ROOT
    replay = build_policy_replay(root)
    candidates = []
    for risk_max, ev_min, runner_min in product((0.35, 0.50, 0.70), (0.0, 10.0, 25.0), (0.10, 0.20)):
        score = (
            float((replay.get("combined_policy_v2") or {}).get("total_pnl") or 0.0)
            - risk_max * 10.0
            + ev_min
            + runner_min * 100.0
        )
        candidates.append(
            {
                "proposal_id": f"offline_r{risk_max}_e{ev_min}_u{runner_min}".replace(".", "_"),
                "thresholds": {"risk_max": risk_max, "ev_min": ev_min, "runner_min": runner_min},
                "expected_metrics": {"score": round(score, 4), "source": "policy_replay"},
                "required_gates": ["policy_replay", "paper_forward", "manual_approval"],
                "live_allowed": False,
            }
        )
    candidates.sort(key=lambda item: item["expected_metrics"]["score"], reverse=True)
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
