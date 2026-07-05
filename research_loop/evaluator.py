from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_loop.api_budget import compare_api_budget, metrics_from_api_budget
from research_loop.objectives import (
    METRIC_SCOPE_COMBINED,
    METRIC_SCOPE_CURRENT_RUN,
    METRIC_SCOPE_HISTORICAL,
    METRIC_SCOPES,
    ObjectiveResult,
    calculate_objective_score,
    metric_delta_is_worse,
)
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
OBJECTIVE_SIGNAL_METRICS = (
    "total_pnl_usd",
    "avg_pnl_pct",
    "median_pnl_pct",
    "win_rate_pct",
    "runner_capture_ratio",
    "runner_capture_ladder_ratio",
    "realized_pnl_on_runners",
    "moonshot_peak100_capture",
    "moonshot_peak500_capture",
    "moonshot_peak1000_capture",
    "moonshot_micro_tail_capture_ratio",
    "severe_loss_count",
    "liquidity_crush_count",
    "adverse_tick_count",
    "stop_loss_count",
    "no_pump_exit_count",
    "max_drawdown_proxy",
    "objective_score",
    "allowed_shadow_followup",
    "simulated_buys",
    "shadow_followup_risk_blocked",
    "shadow_followup_route_proxy",
    "giveback_pct",
    "missed_peak100_count",
    "missed_peak500_count",
    "missed_peak1000_count",
    "api_429_count",
    "provider_degraded_minutes",
    "real_liquidity_breakout_capture",
    "overtrading_count",
    "idle_no_buy_hours",
)
METRIC_VIEW_KEYS = {
    "current_run_metrics": METRIC_SCOPE_CURRENT_RUN,
    "historical_metrics": METRIC_SCOPE_HISTORICAL,
    "combined_metrics": METRIC_SCOPE_COMBINED,
}
REVERSE_METRIC_VIEW_KEYS = {value: key for key, value in METRIC_VIEW_KEYS.items()}


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
    optimized_metric: str | None = None
    optimization_scope: str = METRIC_SCOPE_COMBINED
    current_run_objective: ObjectiveResult | None = None
    historical_objective: ObjectiveResult | None = None
    combined_objective: ObjectiveResult | None = None

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
            "optimized_metric": self.optimized_metric,
            "optimization_scope": self.optimization_scope,
            "current_run_objective": self.current_run_objective.as_dict() if self.current_run_objective else None,
            "historical_objective": self.historical_objective.as_dict() if self.historical_objective else None,
            "combined_objective": self.combined_objective.as_dict() if self.combined_objective else None,
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


def _optimized_metric(candidate_policy: dict[str, Any]) -> str:
    return str(candidate_policy.get("optimized_metric") or "").strip()


def _optimization_scope(candidate_policy: dict[str, Any]) -> str:
    scope = str(candidate_policy.get("optimization_scope") or METRIC_SCOPE_COMBINED).strip()
    return scope or METRIC_SCOPE_COMBINED


def _flat_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in metrics.items()
        if key not in METRIC_VIEW_KEYS and not isinstance(value, (dict, list, tuple, set))
    }


def _metric_views(metrics: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], bool]:
    nested_present = any(isinstance(metrics.get(key), dict) for key in METRIC_VIEW_KEYS)
    flat = _flat_metrics(metrics)
    views: dict[str, dict[str, Any]] = {}
    for payload_key, scope in METRIC_VIEW_KEYS.items():
        payload = metrics.get(payload_key)
        views[scope] = dict(payload) if isinstance(payload, dict) else {}
    if not views[METRIC_SCOPE_COMBINED]:
        views[METRIC_SCOPE_COMBINED] = dict(flat)
    if not nested_present:
        views[METRIC_SCOPE_CURRENT_RUN] = dict(flat)
        views[METRIC_SCOPE_HISTORICAL] = dict(flat)
    elif not views[METRIC_SCOPE_HISTORICAL]:
        views[METRIC_SCOPE_HISTORICAL] = dict(views[METRIC_SCOPE_COMBINED])
    return views, nested_present


def _apply_api_budget_metrics(views: dict[str, dict[str, Any]], api_budget: dict[str, Any] | None) -> None:
    if api_budget is None:
        return
    api_metrics = metrics_from_api_budget(api_budget)
    for payload in views.values():
        payload.update(api_metrics)


def _has_min_comparable_metrics(
    baseline_metrics: dict[str, Any],
    candidate_metrics: dict[str, Any],
) -> bool:
    return all(key in baseline_metrics and key in candidate_metrics for key in MIN_COMPARABLE_METRICS)


def _objective_for_scope(
    baseline_views: dict[str, dict[str, Any]],
    candidate_views: dict[str, dict[str, Any]],
    scope: str,
    *,
    optimized_metric: str,
) -> ObjectiveResult | None:
    baseline = baseline_views.get(scope) or {}
    candidate = candidate_views.get(scope) or {}
    if not _has_min_comparable_metrics(baseline, candidate):
        return None
    return calculate_objective_score(
        baseline,
        candidate,
        optimized_metric=optimized_metric,
        metric_scope=scope,
    )


def _current_run_worsened(objective: ObjectiveResult | None, optimized_metric: str) -> bool:
    if objective is None:
        return False
    if not objective.hard_gate_passed:
        return True
    if objective.score < 0:
        return True
    delta = objective.metric_deltas.get(optimized_metric)
    if delta is not None and metric_delta_is_worse(optimized_metric, float(delta)):
        return True
    return False


def evaluate_replay_candidate(
    candidate_policy: dict[str, Any],
    baseline_metrics: dict[str, Any],
    candidate_metrics: dict[str, Any],
    *,
    baseline_api_budget: dict[str, Any] | None = None,
    candidate_api_budget: dict[str, Any] | None = None,
    safety_result: SafetyResult | None = None,
    min_closed_trades: int = 0,
    min_current_run_closed_trades: int | None = None,
) -> EvaluationResult:
    warnings: list[str] = []
    rejection_reasons: list[str] = []
    proposal_id = _proposal_id(candidate_policy)
    optimized_metric = _optimized_metric(candidate_policy)
    optimization_scope = _optimization_scope(candidate_policy)

    if candidate_metrics.get("failed") is True:
        return EvaluationResult(
            status=STATUS_FAILED,
            accepted=False,
            rejection_reasons=["replay_failed"],
            proposal_id=proposal_id,
            optimized_metric=optimized_metric or None,
            optimization_scope=optimization_scope,
        )

    safety = safety_result or validate_candidate_safety(candidate_policy)
    if not safety.ok:
        return EvaluationResult(
            status=STATUS_REJECTED,
            accepted=False,
            rejection_reasons=[f"safety:{error}" for error in safety.errors],
            warnings=list(safety.warnings),
            proposal_id=proposal_id,
            optimized_metric=optimized_metric or None,
            optimization_scope=optimization_scope,
        )

    if not optimized_metric:
        return EvaluationResult(
            status=STATUS_REJECTED,
            accepted=False,
            rejection_reasons=["missing_optimized_metric"],
            warnings=warnings,
            proposal_id=proposal_id,
            optimized_metric=None,
            optimization_scope=optimization_scope,
        )
    if optimization_scope not in METRIC_SCOPES:
        return EvaluationResult(
            status=STATUS_REJECTED,
            accepted=False,
            rejection_reasons=[f"invalid_optimization_scope:{optimization_scope}"],
            warnings=warnings,
            proposal_id=proposal_id,
            optimized_metric=optimized_metric,
            optimization_scope=optimization_scope,
        )

    baseline_views, baseline_nested = _metric_views(baseline_metrics)
    candidate_views, candidate_nested = _metric_views(candidate_metrics)
    nested_metrics_present = baseline_nested or candidate_nested
    _apply_api_budget_metrics(baseline_views, baseline_api_budget)
    _apply_api_budget_metrics(candidate_views, candidate_api_budget)

    if baseline_api_budget is not None and candidate_api_budget is not None:
        api_comparison = compare_api_budget(baseline_api_budget, candidate_api_budget)
        warnings.extend(api_comparison.warnings)
        if not api_comparison.ok:
            rejection_reasons.extend(api_comparison.rejection_reasons)

    selected_baseline = baseline_views.get(optimization_scope) or {}
    selected_candidate = candidate_views.get(optimization_scope) or {}
    if not _has_min_comparable_metrics(selected_baseline, selected_candidate):
        return EvaluationResult(
            status=STATUS_INCONCLUSIVE,
            accepted=False,
            rejection_reasons=[f"missing_comparable_metrics:{optimization_scope}"],
            warnings=warnings,
            proposal_id=proposal_id,
            optimized_metric=optimized_metric,
            optimization_scope=optimization_scope,
        )

    closed_trades = _closed_trades(selected_candidate)
    if min_closed_trades > 0 and closed_trades < min_closed_trades:
        warnings.append(f"sample_too_small:{closed_trades}<{min_closed_trades}")

    current_run_objective = _objective_for_scope(
        baseline_views,
        candidate_views,
        METRIC_SCOPE_CURRENT_RUN,
        optimized_metric=optimized_metric,
    )
    historical_objective = _objective_for_scope(
        baseline_views,
        candidate_views,
        METRIC_SCOPE_HISTORICAL,
        optimized_metric=optimized_metric,
    )
    combined_objective = _objective_for_scope(
        baseline_views,
        candidate_views,
        METRIC_SCOPE_COMBINED,
        optimized_metric=optimized_metric,
    )
    objective_by_scope = {
        METRIC_SCOPE_CURRENT_RUN: current_run_objective,
        METRIC_SCOPE_HISTORICAL: historical_objective,
        METRIC_SCOPE_COMBINED: combined_objective,
    }
    objective = objective_by_scope[optimization_scope]
    if objective is None:
        return EvaluationResult(
            status=STATUS_INCONCLUSIVE,
            accepted=False,
            rejection_reasons=[f"missing_comparable_metrics:{optimization_scope}"],
            warnings=warnings,
            proposal_id=proposal_id,
            optimized_metric=optimized_metric,
            optimization_scope=optimization_scope,
            current_run_objective=current_run_objective,
            historical_objective=historical_objective,
            combined_objective=combined_objective,
        )

    rejection_reasons.extend(objective.rejection_reasons)
    warnings.extend(objective.warnings)
    if optimized_metric not in objective.metric_deltas:
        rejection_reasons.append(f"optimized_metric_missing:{optimized_metric}")
    if _current_run_worsened(current_run_objective, optimized_metric):
        rejection_reasons.append("current_run_regression")
    no_metric_delta = not any(abs(float(objective.metric_deltas.get(key) or 0.0)) > 1e-12 for key in OBJECTIVE_SIGNAL_METRICS)
    if no_metric_delta:
        warnings.append("replay_no_objective_metric_delta")
        if candidate_policy.get("changes"):
            warnings.append("no_effect_detected")

    effective_min_current = min_current_run_closed_trades
    if effective_min_current is None:
        effective_min_current = 1 if nested_metrics_present else 0
    current_run_closed_trades = _closed_trades(candidate_views.get(METRIC_SCOPE_CURRENT_RUN) or {})
    current_run_sample_too_small = effective_min_current > 0 and current_run_closed_trades < effective_min_current
    if current_run_sample_too_small:
        warnings.append(f"current_run_sample_too_small:{current_run_closed_trades}<{effective_min_current}")

    if rejection_reasons:
        return EvaluationResult(
            status=STATUS_REJECTED,
            accepted=False,
            objective=objective,
            rejection_reasons=rejection_reasons,
            warnings=warnings,
            proposal_id=proposal_id,
            optimized_metric=optimized_metric,
            optimization_scope=optimization_scope,
            current_run_objective=current_run_objective,
            historical_objective=historical_objective,
            combined_objective=combined_objective,
        )
    if min_closed_trades > 0 and closed_trades < min_closed_trades or current_run_sample_too_small:
        return EvaluationResult(
            status=STATUS_NEEDS_PAPER,
            accepted=False,
            needs_paper=True,
            objective=objective,
            warnings=warnings,
            proposal_id=proposal_id,
            optimized_metric=optimized_metric,
            optimization_scope=optimization_scope,
            current_run_objective=current_run_objective,
            historical_objective=historical_objective,
            combined_objective=combined_objective,
        )
    return EvaluationResult(
        status=STATUS_ACCEPTED_REPLAY,
        accepted=True,
        objective=objective,
        warnings=warnings,
        proposal_id=proposal_id,
        optimized_metric=optimized_metric,
        optimization_scope=optimization_scope,
        current_run_objective=current_run_objective,
        historical_objective=historical_objective,
        combined_objective=combined_objective,
    )


def evaluate_replay_run(
    run_dir: str | Path,
    baseline_metrics: dict[str, Any] | str | Path | None = None,
    *,
    baseline_api_budget: dict[str, Any] | None = None,
    candidate_api_budget: dict[str, Any] | None = None,
    min_closed_trades: int = 0,
    min_current_run_closed_trades: int | None = None,
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
        min_current_run_closed_trades=min_current_run_closed_trades,
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
        optimized_metric=result.optimized_metric,
        optimization_scope=result.optimization_scope,
        current_run_objective=result.current_run_objective,
        historical_objective=result.historical_objective,
        combined_objective=result.combined_objective,
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
