from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_loop.api_budget import build_api_budget_report, compare_api_budget, metrics_from_api_budget
from research_loop.evaluator import STATUS_ACCEPTED_REPLAY, EvaluationResult
from research_loop.experiment_schema import validate_candidate_policy
from research_loop.objectives import ObjectiveResult, calculate_objective_score
from research_loop.paths import metrics_dir, project_root, research_runs_dir
from research_loop.policy_promoter import DEFAULT_SOURCE_PROFILE, PromotionResult, promote_to_paper_candidate
from research_loop.scoreboard import record_evaluation

STATUS_PAPER_FORWARD_STARTED = "paper_forward_started"
STATUS_ACCEPTED_PAPER = "accepted_paper"
STATUS_REJECTED_PAPER = "rejected_paper"

PAPER_FORWARD_BUDGET = {
    "min_hours": 6,
    "max_hours": 24,
    "min_closed_trades": 5,
    "min_decisions": 100,
    "max_daily_buys": 15,
    "api_budget_ok_required": True,
}

COMPARABLE_METRICS = (
    "total_pnl_usd",
    "median_pnl_pct",
    "runner_capture_ratio",
)


class PaperForwardError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperForwardStartResult:
    run_id: str
    run_dir: Path
    status: str
    candidate_policy_path: Path
    state_path: Path
    budget_path: Path
    promotion: PromotionResult
    budget: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "status": self.status,
            "candidate_policy_path": str(self.candidate_policy_path),
            "state_path": str(self.state_path),
            "budget_path": str(self.budget_path),
            "promotion": self.promotion.as_dict(),
            "budget": dict(self.budget),
        }


@dataclass(frozen=True)
class PaperForwardResult:
    run_id: str
    run_dir: Path
    status: str
    accepted: bool
    objective: ObjectiveResult | None = None
    rejection_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    paper_metrics: dict[str, Any] = field(default_factory=dict)
    baseline_metrics: dict[str, Any] = field(default_factory=dict)
    api_budget: dict[str, Any] = field(default_factory=dict)
    result_path: Path | None = None
    rollback_report_path: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "status": self.status,
            "accepted": self.accepted,
            "objective": self.objective.as_dict() if self.objective else None,
            "rejection_reasons": list(self.rejection_reasons),
            "warnings": list(self.warnings),
            "paper_metrics": dict(self.paper_metrics),
            "baseline_metrics": dict(self.baseline_metrics),
            "api_budget": dict(self.api_budget),
            "result_path": str(self.result_path) if self.result_path else None,
            "rollback_report_path": str(self.rollback_report_path) if self.rollback_report_path else None,
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-").lower()
    return cleaned or "paper_forward"


def _default_run_id(proposal_id: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    return _safe_id(f"paper_{proposal_id}_{stamp}")


def _paper_forward_root(root: Path) -> Path:
    return research_runs_dir(root) / "paper_forward"


def _resolve_run_dir(run_id_or_dir: str | Path, root: Path) -> Path:
    candidate = Path(run_id_or_dir)
    if candidate.exists() or candidate.is_absolute() or len(candidate.parts) > 1:
        return candidate
    return _paper_forward_root(root) / str(run_id_or_dir)


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _float_metric(metrics: dict[str, Any], *keys: str) -> float:
    for key in keys:
        if key not in metrics:
            continue
        try:
            return float(metrics.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
    return 0.0


def _int_metric(metrics: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key not in metrics:
            continue
        try:
            return int(float(metrics.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return 0


def _merge_budget(overrides: dict[str, Any] | None) -> dict[str, Any]:
    budget = dict(PAPER_FORWARD_BUDGET)
    if overrides:
        budget.update(overrides)
    return budget


def _load_current_paper_metrics(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    summary = _read_json(metrics_dir(root) / "current_run_summary.json")
    diagnostics = _read_json(metrics_dir(root) / "current_run_trade_diagnostics.json")
    lane_summary = _read_json(metrics_dir(root) / "current_run_lane_summary.json")

    merged: dict[str, Any] = {}
    for payload in (summary, diagnostics, lane_summary):
        if isinstance(payload, dict):
            merged.update(payload)

    started_at = state.get("started_at_utc")
    elapsed_hours = _float_metric(merged, "elapsed_hours", "run_hours", "hours")
    if elapsed_hours <= 0 and isinstance(started_at, str):
        try:
            started = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            elapsed_hours = max(0.0, (dt.datetime.now(dt.timezone.utc) - started).total_seconds() / 3600.0)
        except ValueError:
            elapsed_hours = 0.0

    return {
        **merged,
        "elapsed_hours": elapsed_hours,
        "closed_trades": _int_metric(merged, "closed_trades", "trades", "closed_positions"),
        "decisions": _int_metric(merged, "decisions", "decision_count", "total_decisions"),
        "daily_buys": _int_metric(merged, "daily_buys", "buy_count", "buys", "buys_today"),
        "total_pnl_usd": _float_metric(merged, "total_pnl_usd", "total_pnl", "realized_pnl_usd"),
        "avg_pnl_pct": _float_metric(merged, "avg_pnl_pct", "avg_pnl"),
        "median_pnl_pct": _float_metric(merged, "median_pnl_pct", "median_pnl"),
        "win_rate_pct": _float_metric(merged, "win_rate_pct", "win_rate"),
        "runner_capture_ratio": _float_metric(merged, "runner_capture_ratio"),
        "severe_loss_count": _int_metric(merged, "severe_loss_count"),
        "liquidity_crush_count": _int_metric(merged, "liquidity_crush_count"),
        "adverse_tick_count": _int_metric(merged, "adverse_tick_count"),
        "no_pump_exit_count": _int_metric(merged, "no_pump_exit_count"),
        "max_drawdown_proxy": _float_metric(merged, "max_drawdown_proxy", "max_drawdown"),
    }


def _load_baseline_metrics(run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    if isinstance(state.get("baseline_metrics"), dict):
        return dict(state["baseline_metrics"])
    payload = _read_json(run_dir / "baseline_metrics.json")
    return payload if isinstance(payload, dict) else {}


def _has_comparable_metrics(baseline_metrics: dict[str, Any], paper_metrics: dict[str, Any]) -> bool:
    return all(key in baseline_metrics and key in paper_metrics for key in COMPARABLE_METRICS)


def _api_budget_ok(
    api_budget: dict[str, Any],
    baseline_api_budget: dict[str, Any] | None,
) -> tuple[bool, list[str], list[str], dict[str, Any]]:
    warnings: list[str] = []
    rejection_reasons: list[str] = []
    merged_api_budget = dict(api_budget)
    if baseline_api_budget is not None:
        comparison = compare_api_budget(baseline_api_budget, api_budget)
        merged_api_budget["comparison"] = comparison.as_dict()
        warnings.extend(comparison.warnings)
        rejection_reasons.extend(comparison.rejection_reasons)
        return comparison.ok, rejection_reasons, warnings, merged_api_budget

    comparison = api_budget.get("comparison")
    if isinstance(comparison, dict):
        ok = comparison.get("ok")
        if ok is False:
            reasons = comparison.get("rejection_reasons") or []
            rejection_reasons.extend(str(reason) for reason in reasons)
            return False, rejection_reasons, warnings, merged_api_budget

    budget_metrics = metrics_from_api_budget(api_budget)
    if budget_metrics.get("api_429_count", 0.0) > 0:
        rejection_reasons.append("api_budget:api_429_count>0")
    if budget_metrics.get("provider_degraded_minutes", 0.0) > 0:
        rejection_reasons.append("api_budget:provider_degraded_minutes>0")
    return not rejection_reasons, rejection_reasons, warnings, merged_api_budget


def _budget_rejections(metrics: dict[str, Any], budget: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    elapsed_hours = _float_metric(metrics, "elapsed_hours")
    closed_trades = _int_metric(metrics, "closed_trades")
    decisions = _int_metric(metrics, "decisions")
    daily_buys = _int_metric(metrics, "daily_buys")

    if elapsed_hours < float(budget.get("min_hours") or 0):
        reasons.append(f"paper_budget:min_hours:{elapsed_hours:g}<{budget.get('min_hours')}")
    if closed_trades < int(budget.get("min_closed_trades") or 0):
        reasons.append(f"paper_budget:min_closed_trades:{closed_trades}<{budget.get('min_closed_trades')}")
    if decisions < int(budget.get("min_decisions") or 0):
        reasons.append(f"paper_budget:min_decisions:{decisions}<{budget.get('min_decisions')}")
    if daily_buys > int(budget.get("max_daily_buys") or 0):
        reasons.append(f"paper_budget:max_daily_buys:{daily_buys}>{budget.get('max_daily_buys')}")
    return reasons


def start_paper_forward(
    candidate_policy: str | Path | dict[str, Any],
    *,
    root: str | Path | None = None,
    run_id: str | None = None,
    evaluation_result: EvaluationResult | dict[str, Any] | str | None = STATUS_ACCEPTED_REPLAY,
    budget: dict[str, Any] | None = None,
    source_profile: str = DEFAULT_SOURCE_PROFILE,
    profile_id: str | None = None,
    allow_needs_paper: bool = False,
) -> PaperForwardStartResult:
    resolved_root = project_root(root)
    policy = validate_candidate_policy(candidate_policy)
    resolved_run_id = _safe_id(run_id or _default_run_id(policy.proposal_id))
    run_dir = _paper_forward_root(resolved_root) / resolved_run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    candidate_policy_path = run_dir / "candidate_policy.json"
    promotion_report_path = run_dir / "promotion_report.json"
    budget_path = run_dir / "paper_budget.json"
    state_path = run_dir / "paper_forward_state.json"
    baseline_metrics_path = run_dir / "baseline_metrics.json"
    baseline_api_budget_path = run_dir / "baseline_api_budget.json"
    resolved_budget = _merge_budget(budget)

    promotion = promote_to_paper_candidate(
        policy.to_dict(),
        evaluation_result=evaluation_result,
        root=resolved_root,
        profile_id=profile_id or policy.proposal_id,
        source_profile=source_profile,
        promotion_report_path=promotion_report_path,
        allow_needs_paper=allow_needs_paper,
    )

    started_at = utc_now()
    initial_state = {
        "run_id": resolved_run_id,
        "started_at_utc": started_at,
    }
    baseline_metrics = _load_current_paper_metrics(resolved_root, initial_state)
    baseline_api_budget = build_api_budget_report(resolved_root, write=True)

    _write_json(candidate_policy_path, policy.to_dict())
    _write_json(budget_path, resolved_budget)
    _write_json(baseline_metrics_path, baseline_metrics)
    _write_json(baseline_api_budget_path, baseline_api_budget)
    state = {
        "run_id": resolved_run_id,
        "status": STATUS_PAPER_FORWARD_STARTED,
        "started_at_utc": started_at,
        "candidate_policy_path": str(candidate_policy_path),
        "budget_path": str(budget_path),
        "baseline_metrics_path": str(baseline_metrics_path),
        "baseline_api_budget_path": str(baseline_api_budget_path),
        "promotion": promotion.as_dict(),
        "paper_profile": promotion.profile_name,
        "paper_profile_path": str(promotion.profile_path),
        "budget": resolved_budget,
    }
    _write_json(state_path, state)

    return PaperForwardStartResult(
        run_id=resolved_run_id,
        run_dir=run_dir,
        status=STATUS_PAPER_FORWARD_STARTED,
        candidate_policy_path=candidate_policy_path,
        state_path=state_path,
        budget_path=budget_path,
        promotion=promotion,
        budget=resolved_budget,
    )


def finalize_paper_forward(
    run_id_or_dir: str | Path,
    *,
    root: str | Path | None = None,
    paper_metrics: dict[str, Any] | None = None,
    baseline_metrics: dict[str, Any] | None = None,
    api_budget: dict[str, Any] | None = None,
    baseline_api_budget: dict[str, Any] | None = None,
    rollback_on_reject: bool = True,
) -> PaperForwardResult:
    resolved_root = project_root(root)
    run_dir = _resolve_run_dir(run_id_or_dir, resolved_root)
    state_path = run_dir / "paper_forward_state.json"
    state = _read_json(state_path)
    if not isinstance(state, dict):
        raise PaperForwardError(f"paper_forward_state_missing:{state_path}")

    candidate_policy = _read_json(run_dir / "candidate_policy.json")
    if not isinstance(candidate_policy, dict):
        raise PaperForwardError(f"candidate_policy_missing:{run_dir / 'candidate_policy.json'}")

    budget = dict(state.get("budget") if isinstance(state.get("budget"), dict) else PAPER_FORWARD_BUDGET)
    resolved_paper_metrics = dict(paper_metrics) if paper_metrics is not None else _load_current_paper_metrics(resolved_root, state)
    resolved_baseline_metrics = (
        dict(baseline_metrics) if baseline_metrics is not None else _load_baseline_metrics(run_dir, state)
    )
    resolved_api_budget = dict(api_budget) if api_budget is not None else build_api_budget_report(resolved_root, write=True)
    resolved_baseline_api_budget = baseline_api_budget
    if resolved_baseline_api_budget is None:
        payload = _read_json(run_dir / "baseline_api_budget.json")
        resolved_baseline_api_budget = payload if isinstance(payload, dict) else None

    warnings: list[str] = []
    rejection_reasons = _budget_rejections(resolved_paper_metrics, budget)
    objective: ObjectiveResult | None = None

    if bool(budget.get("api_budget_ok_required", True)):
        api_ok, api_reasons, api_warnings, resolved_api_budget = _api_budget_ok(
            resolved_api_budget,
            resolved_baseline_api_budget,
        )
        warnings.extend(api_warnings)
        if not api_ok:
            rejection_reasons.extend(api_reasons)

    if not resolved_baseline_metrics:
        rejection_reasons.append("missing_baseline_metrics")
    elif not _has_comparable_metrics(resolved_baseline_metrics, resolved_paper_metrics):
        rejection_reasons.append("missing_comparable_paper_metrics")
    else:
        baseline_for_objective = dict(resolved_baseline_metrics)
        paper_for_objective = dict(resolved_paper_metrics)
        if resolved_baseline_api_budget is not None:
            baseline_for_objective.update(metrics_from_api_budget(resolved_baseline_api_budget))
        paper_for_objective.update(metrics_from_api_budget(resolved_api_budget))
        objective = calculate_objective_score(baseline_for_objective, paper_for_objective)
        warnings.extend(objective.warnings)
        rejection_reasons.extend(objective.rejection_reasons)

    status = STATUS_REJECTED_PAPER if rejection_reasons else STATUS_ACCEPTED_PAPER
    result_path = run_dir / "paper_forward_result.json"
    rollback_report_path: Path | None = None

    result = PaperForwardResult(
        run_id=str(state.get("run_id") or run_dir.name),
        run_dir=run_dir,
        status=status,
        accepted=status == STATUS_ACCEPTED_PAPER,
        objective=objective,
        rejection_reasons=sorted(set(rejection_reasons)),
        warnings=warnings,
        paper_metrics=resolved_paper_metrics,
        baseline_metrics=resolved_baseline_metrics,
        api_budget=resolved_api_budget,
        result_path=result_path,
    )
    _write_json(result_path, result.as_dict())

    state["status"] = status
    state["finalized_at_utc"] = utc_now()
    state["result_path"] = str(result_path)
    state["rejection_reasons"] = result.rejection_reasons
    _write_json(state_path, state)

    record_evaluation(
        run_id=result.run_id,
        candidate_policy=candidate_policy,
        evaluation_result=EvaluationResult(
            status=status,
            accepted=result.accepted,
            objective=objective,
            rejection_reasons=result.rejection_reasons,
            warnings=result.warnings,
            run_id=result.run_id,
            proposal_id=str(candidate_policy.get("proposal_id") or ""),
        ),
        root=resolved_root,
    )

    if status == STATUS_REJECTED_PAPER and rollback_on_reject:
        from research_loop.rollback import rollback_paper_candidate

        rollback = rollback_paper_candidate(run_dir, root=resolved_root, reason="paper_forward_rejected")
        rollback_report_path = rollback.rollback_report_path
        result = PaperForwardResult(
            run_id=result.run_id,
            run_dir=result.run_dir,
            status=result.status,
            accepted=result.accepted,
            objective=result.objective,
            rejection_reasons=result.rejection_reasons,
            warnings=result.warnings,
            paper_metrics=result.paper_metrics,
            baseline_metrics=result.baseline_metrics,
            api_budget=result.api_budget,
            result_path=result.result_path,
            rollback_report_path=rollback_report_path,
        )
        _write_json(result_path, result.as_dict())

    return result


__all__ = [
    "PAPER_FORWARD_BUDGET",
    "PaperForwardError",
    "PaperForwardResult",
    "PaperForwardStartResult",
    "STATUS_ACCEPTED_PAPER",
    "STATUS_PAPER_FORWARD_STARTED",
    "STATUS_REJECTED_PAPER",
    "finalize_paper_forward",
    "start_paper_forward",
]
