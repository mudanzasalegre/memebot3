from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_loop.api_budget import compare_api_budget, metrics_from_api_budget
from research_loop.objectives import ObjectiveResult, calculate_objective_score
from research_loop.safety import SafetyResult, validate_candidate_safety

STATUS_ACCEPTED_REPLAY = "accepted_replay"
STATUS_NEEDS_PAPER = "needs_paper"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
STATUS_INCONCLUSIVE = "inconclusive"
EVALUATION_STATUSES = {
    STATUS_ACCEPTED_REPLAY,
    STATUS_NEEDS_PAPER,
    STATUS_REJECTED,
    STATUS_FAILED,
    STATUS_INCONCLUSIVE,
}
MIN_COMPARABLE_METRICS = (
    "total_pnl_usd",
    "median_pnl_pct",
    "runner_capture_ratio",
)


@dataclass(frozen=True)
class EvaluationResult:
    status: str
    accepted: bool
    needs_paper: bool = False
    objective: ObjectiveResult | None = None
    rejection_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    run_id: str | None = None
    proposal_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "accepted": self.accepted,
            "needs_paper": self.needs_paper,
            "objective": self.objective.as_dict() if self.objective else None,
            "rejection_reasons": list(self.rejection_reasons),
            "warnings": list(self.warnings),
            "run_id": self.run_id,
            "proposal_id": self.proposal_id,
        }


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def _closed_trades(metrics: dict[str, Any]) -> int:
    try:
        return int(float(metrics.get("closed_trades") or metrics.get("trades") or 0))
    except (TypeError, ValueError):
        return 0


def _proposal_id(candidate_policy: dict[str, Any]) -> str | None:
    raw = candidate_policy.get("proposal_id")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _has_min_comparable_metrics(
    baseline_metrics: dict[str, Any],
    candidate_metrics: dict[str, Any],
) -> bool:
    return all(key in baseline_metrics and key in candidate_metrics for key in MIN_COMPARABLE_METRICS)


def evaluate_replay_candidate(
    candidate_policy: dict[str, Any],
    baseline_metrics: dict[str, Any],
    candidate_metrics: dict[str, Any],
    *,
    baseline_api_budget: dict[str, Any] | None = None,
    candidate_api_budget: dict[str, Any] | None = None,
    safety_result: SafetyResult | None = None,
    min_closed_trades: int = 0,
) -> EvaluationResult:
    warnings: list[str] = []
    rejection_reasons: list[str] = []
    proposal_id = _proposal_id(candidate_policy)

    if candidate_metrics.get("failed") is True:
        return EvaluationResult(
            status=STATUS_FAILED,
            accepted=False,
            rejection_reasons=["replay_failed"],
            proposal_id=proposal_id,
        )

    safety = safety_result or validate_candidate_safety(candidate_policy)
    if not safety.ok:
        return EvaluationResult(
            status=STATUS_REJECTED,
            accepted=False,
            rejection_reasons=[f"safety:{error}" for error in safety.errors],
            warnings=list(safety.warnings),
            proposal_id=proposal_id,
        )

    merged_baseline = dict(baseline_metrics)
    merged_candidate = dict(candidate_metrics)
    if baseline_api_budget is not None:
        merged_baseline.update(metrics_from_api_budget(baseline_api_budget))
    if candidate_api_budget is not None:
        merged_candidate.update(metrics_from_api_budget(candidate_api_budget))

    if baseline_api_budget is not None and candidate_api_budget is not None:
        api_comparison = compare_api_budget(baseline_api_budget, candidate_api_budget)
        warnings.extend(api_comparison.warnings)
        if not api_comparison.ok:
            rejection_reasons.extend(api_comparison.rejection_reasons)

    if not _has_min_comparable_metrics(merged_baseline, merged_candidate):
        return EvaluationResult(
            status=STATUS_INCONCLUSIVE,
            accepted=False,
            rejection_reasons=["missing_comparable_metrics"],
            warnings=warnings,
            proposal_id=proposal_id,
        )

    closed_trades = _closed_trades(merged_candidate)
    if min_closed_trades > 0 and closed_trades < min_closed_trades:
        warnings.append(f"sample_too_small:{closed_trades}<{min_closed_trades}")

    objective = calculate_objective_score(merged_baseline, merged_candidate)
    rejection_reasons.extend(objective.rejection_reasons)
    warnings.extend(objective.warnings)

    if rejection_reasons:
        return EvaluationResult(
            status=STATUS_REJECTED,
            accepted=False,
            objective=objective,
            rejection_reasons=rejection_reasons,
            warnings=warnings,
            proposal_id=proposal_id,
        )
    if min_closed_trades > 0 and closed_trades < min_closed_trades:
        return EvaluationResult(
            status=STATUS_NEEDS_PAPER,
            accepted=False,
            needs_paper=True,
            objective=objective,
            warnings=warnings,
            proposal_id=proposal_id,
        )
    return EvaluationResult(
        status=STATUS_ACCEPTED_REPLAY,
        accepted=True,
        objective=objective,
        warnings=warnings,
        proposal_id=proposal_id,
    )


def evaluate_replay_run(
    run_dir: str | Path,
    baseline_metrics: dict[str, Any] | str | Path | None = None,
    *,
    baseline_api_budget: dict[str, Any] | None = None,
    candidate_api_budget: dict[str, Any] | None = None,
    min_closed_trades: int = 0,
) -> EvaluationResult:
    resolved_run_dir = Path(run_dir)
    candidate_policy = _read_json(resolved_run_dir / "candidate_policy.json")
    candidate_metrics = _read_json(resolved_run_dir / "replay_metrics.json")
    run_id = resolved_run_dir.name
    if not isinstance(candidate_policy, dict):
        return EvaluationResult(
            status=STATUS_FAILED,
            accepted=False,
            rejection_reasons=["missing_candidate_policy"],
            run_id=run_id,
        )
    if not isinstance(candidate_metrics, dict):
        return EvaluationResult(
            status=STATUS_FAILED,
            accepted=False,
            rejection_reasons=["missing_replay_metrics"],
            run_id=run_id,
            proposal_id=_proposal_id(candidate_policy),
        )

    if baseline_metrics is None:
        local_baseline = _read_json(resolved_run_dir / "baseline_metrics.json")
        if not isinstance(local_baseline, dict):
            return EvaluationResult(
                status=STATUS_INCONCLUSIVE,
                accepted=False,
                rejection_reasons=["missing_baseline_metrics"],
                run_id=run_id,
                proposal_id=_proposal_id(candidate_policy),
            )
        baseline_payload = local_baseline
    elif isinstance(baseline_metrics, (str, Path)):
        baseline_payload = _read_json(Path(baseline_metrics))
        if not isinstance(baseline_payload, dict):
            return EvaluationResult(
                status=STATUS_INCONCLUSIVE,
                accepted=False,
                rejection_reasons=["missing_baseline_metrics"],
                run_id=run_id,
                proposal_id=_proposal_id(candidate_policy),
            )
    else:
        baseline_payload = dict(baseline_metrics)

    result = evaluate_replay_candidate(
        candidate_policy,
        baseline_payload,
        candidate_metrics,
        baseline_api_budget=baseline_api_budget,
        candidate_api_budget=candidate_api_budget,
        min_closed_trades=min_closed_trades,
    )
    return EvaluationResult(
        status=result.status,
        accepted=result.accepted,
        needs_paper=result.needs_paper,
        objective=result.objective,
        rejection_reasons=result.rejection_reasons,
        warnings=result.warnings,
        run_id=run_id,
        proposal_id=result.proposal_id,
    )


__all__ = [
    "EVALUATION_STATUSES",
    "EvaluationResult",
    "STATUS_ACCEPTED_REPLAY",
    "STATUS_FAILED",
    "STATUS_INCONCLUSIVE",
    "STATUS_NEEDS_PAPER",
    "STATUS_REJECTED",
    "evaluate_replay_candidate",
    "evaluate_replay_run",
]
