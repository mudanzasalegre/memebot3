from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from research_loop.bandit import DEFAULT_SPACES, suggest_spaces
from research_loop.batch_runner import BatchRunResult, run_research_batch
from research_loop.evaluator import EvaluationResult, STATUS_ACCEPTED_REPLAY
from research_loop.objectives import ObjectiveResult, calculate_objective_score
from research_loop.paper_forward import STATUS_ACCEPTED_PAPER, STATUS_PAPER_FORWARD_STARTED, STATUS_REJECTED_PAPER, start_paper_forward
from research_loop.paths import project_root, research_runs_dir
from research_loop.report_bundle import build_report_bundle
from research_loop.rollback import RollbackResult, rollback_paper_candidate
from research_loop.scoreboard import load_scoreboard, record_evaluation

IDLE_FOCUS_SPACES = ("shadow_followup_micro", "paper_exploration", "moonshot_micro")
ACTIVE_PAPER_STATUSES = {STATUS_PAPER_FORWARD_STARTED, STATUS_ACCEPTED_PAPER}
AUTORESEARCH_CONFIG_DEFAULTS = {
    "enabled": True,
    "mode": "paper_replay",
    "interval_hours": 6.0,
    "max_candidates_per_cycle": 25,
    "max_parallel": 1,
    "api_budget_aware": True,
    "live_promotion_enabled": False,
    "auto_paper_promote": True,
    "auto_live_promote": False,
    "idle_threshold_hours": 3.0,
    "regenerate_reports": False,
    "batch_mode": "seeded_random",
    "space": None,
    "profitability_demotion_enabled": True,
}


class AutoResearchSchedulerError(RuntimeError):
    pass


@dataclass(frozen=True)
class AutoResearchConfig:
    enabled: bool = True
    mode: str = "paper_replay"
    interval_hours: float = 6.0
    max_candidates_per_cycle: int = 25
    max_parallel: int = 1
    api_budget_aware: bool = True
    live_promotion_enabled: bool = False
    auto_paper_promote: bool = True
    auto_live_promote: bool = False
    idle_threshold_hours: float = 3.0
    regenerate_reports: bool = False
    batch_mode: str = "seeded_random"
    space: str | None = None
    profitability_demotion_enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "interval_hours": self.interval_hours,
            "max_candidates_per_cycle": self.max_candidates_per_cycle,
            "max_parallel": self.max_parallel,
            "api_budget_aware": self.api_budget_aware,
            "live_promotion_enabled": self.live_promotion_enabled,
            "auto_paper_promote": self.auto_paper_promote,
            "auto_live_promote": self.auto_live_promote,
            "idle_threshold_hours": self.idle_threshold_hours,
            "regenerate_reports": self.regenerate_reports,
            "batch_mode": self.batch_mode,
            "space": self.space,
            "profitability_demotion_enabled": self.profitability_demotion_enabled,
        }


@dataclass(frozen=True)
class IdleTrigger:
    active: bool
    idle_hours: float
    focus_spaces: tuple[str, ...] = IDLE_FOCUS_SPACES
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "idle_hours": self.idle_hours,
            "focus_spaces": list(self.focus_spaces),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class SpaceSelection:
    spaces: list[str]
    idle_trigger: IdleTrigger
    mode: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "spaces": list(self.spaces),
            "idle_trigger": self.idle_trigger.as_dict(),
            "mode": self.mode,
        }


@dataclass(frozen=True)
class PaperDemotionResult:
    checked: bool
    run_id: str | None
    status: str
    degraded: bool = False
    rejection_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    objective: ObjectiveResult | None = None
    rollback: RollbackResult | None = None
    demotion_report_path: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "run_id": self.run_id,
            "status": self.status,
            "degraded": self.degraded,
            "rejection_reasons": list(self.rejection_reasons),
            "warnings": list(self.warnings),
            "objective": self.objective.as_dict() if self.objective else None,
            "rollback": self.rollback.as_dict() if self.rollback else None,
            "demotion_report_path": str(self.demotion_report_path) if self.demotion_report_path else None,
        }


@dataclass(frozen=True)
class AutoResearchCycleResult:
    cycle_id: str
    status: str
    config: AutoResearchConfig
    selected_spaces: list[str]
    idle_trigger: IdleTrigger
    report_bundle_path: Path | None
    batches: list[Any] = field(default_factory=list)
    paper_forward_start: dict[str, Any] | None = None
    demotion: PaperDemotionResult | None = None
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    cycle_report_path: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "status": self.status,
            "config": self.config.as_dict(),
            "selected_spaces": list(self.selected_spaces),
            "idle_trigger": self.idle_trigger.as_dict(),
            "report_bundle_path": str(self.report_bundle_path) if self.report_bundle_path else None,
            "batches": [_as_dict(batch) for batch in self.batches],
            "paper_forward_start": self.paper_forward_start,
            "demotion": self.demotion.as_dict() if self.demotion else None,
            "warnings": list(self.warnings),
            "failures": list(self.failures),
            "cycle_report_path": str(self.cycle_report_path) if self.cycle_report_path else None,
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _as_dict(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return value


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


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_value(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_scheduler_config(
    env: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> AutoResearchConfig:
    source = os.environ if env is None else env
    defaults = dict(AUTORESEARCH_CONFIG_DEFAULTS)
    values = {
        "enabled": _bool_value(source.get("AUTORESEARCH_ENABLED"), bool(defaults["enabled"])),
        "mode": str(source.get("AUTORESEARCH_MODE") or defaults["mode"]),
        "interval_hours": _float_value(source.get("AUTORESEARCH_INTERVAL_HOURS"), float(defaults["interval_hours"])),
        "max_candidates_per_cycle": _int_value(
            source.get("AUTORESEARCH_MAX_CANDIDATES_PER_CYCLE"),
            int(defaults["max_candidates_per_cycle"]),
        ),
        "max_parallel": _int_value(source.get("AUTORESEARCH_MAX_PARALLEL"), int(defaults["max_parallel"])),
        "api_budget_aware": _bool_value(
            source.get("AUTORESEARCH_API_BUDGET_AWARE"),
            bool(defaults["api_budget_aware"]),
        ),
        "live_promotion_enabled": _bool_value(
            source.get("AUTORESEARCH_LIVE_PROMOTION_ENABLED"),
            bool(defaults["live_promotion_enabled"]),
        ),
        "auto_paper_promote": _bool_value(
            source.get("AUTORESEARCH_AUTO_PAPER_PROMOTE"),
            bool(defaults["auto_paper_promote"]),
        ),
        "auto_live_promote": _bool_value(
            source.get("AUTORESEARCH_AUTO_LIVE_PROMOTE"),
            bool(defaults["auto_live_promote"]),
        ),
        "idle_threshold_hours": _float_value(
            source.get("AUTORESEARCH_IDLE_THRESHOLD_HOURS"),
            float(defaults["idle_threshold_hours"]),
        ),
        "regenerate_reports": _bool_value(
            source.get("AUTORESEARCH_REGENERATE_REPORTS"),
            bool(defaults["regenerate_reports"]),
        ),
        "batch_mode": str(source.get("AUTORESEARCH_BATCH_MODE") or defaults["batch_mode"]),
        "space": source.get("AUTORESEARCH_SPACE") or defaults["space"],
        "profitability_demotion_enabled": _bool_value(
            source.get("AUTORESEARCH_PROFITABILITY_DEMOTION_ENABLED"),
            bool(defaults["profitability_demotion_enabled"]),
        ),
    }
    if overrides:
        values.update(dict(overrides))
    config = AutoResearchConfig(
        enabled=_bool_value(values["enabled"], True),
        mode=str(values["mode"]),
        interval_hours=max(0.01, _float_value(values["interval_hours"], 6.0)),
        max_candidates_per_cycle=max(1, _int_value(values["max_candidates_per_cycle"], 25)),
        max_parallel=max(1, _int_value(values["max_parallel"], 1)),
        api_budget_aware=_bool_value(values["api_budget_aware"], True),
        live_promotion_enabled=_bool_value(values["live_promotion_enabled"], False),
        auto_paper_promote=_bool_value(values["auto_paper_promote"], True),
        auto_live_promote=_bool_value(values["auto_live_promote"], False),
        idle_threshold_hours=max(0.0, _float_value(values["idle_threshold_hours"], 3.0)),
        regenerate_reports=_bool_value(values["regenerate_reports"], False),
        batch_mode=str(values["batch_mode"]),
        space=str(values["space"]) if values.get("space") else None,
        profitability_demotion_enabled=_bool_value(values["profitability_demotion_enabled"], True),
    )
    validate_scheduler_config(config)
    return config


def validate_scheduler_config(config: AutoResearchConfig) -> None:
    errors: list[str] = []
    if config.live_promotion_enabled:
        errors.append("AUTORESEARCH_LIVE_PROMOTION_ENABLED_must_be_false")
    if config.auto_live_promote:
        errors.append("AUTORESEARCH_AUTO_LIVE_PROMOTE_must_be_false")
    if config.mode not in {"paper_replay", "replay", "paper"}:
        errors.append(f"unsupported_autoresearch_mode:{config.mode}")
    if errors:
        raise AutoResearchSchedulerError(";".join(errors))


def _nested_dict(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict):
            return {}
        value = value.get(key)
    return value if isinstance(value, dict) else {}


def _metric_float(metrics: dict[str, Any], *keys: str) -> float:
    for key in keys:
        if key not in metrics:
            continue
        try:
            return float(metrics.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
    return 0.0


def _metric_int(metrics: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key not in metrics:
            continue
        try:
            return int(float(metrics.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return 0


def detect_idle_trigger(report_bundle: dict[str, Any], *, idle_threshold_hours: float = 3.0) -> IdleTrigger:
    summary = _nested_dict(report_bundle, "current_run", "summary")
    recommendation = _nested_dict(report_bundle, "recommendation_context")
    idle_hours = max(
        _metric_float(summary, "idle_no_buy_hours", "hours_since_last_buy", "no_buy_hours", "idle_hours", "hours_without_buys"),
        _metric_float(recommendation, "idle_no_buy_hours", "hours_since_last_buy", "idle_hours"),
    )
    run_hours = _metric_float(summary, "run_hours", "elapsed_hours", "hours")
    buys = _metric_int(summary, "buys", "buy_count", "daily_buys", "buys_today")
    closed_positions = _metric_int(summary, "closed_positions", "closed_trades")
    decisions = _metric_int(summary, "strategy_decisions", "decisions", "decision_count")
    reasons: list[str] = []

    if idle_hours <= 0 and run_hours >= idle_threshold_hours and buys == 0 and closed_positions == 0:
        idle_hours = run_hours
        reasons.append("run_hours_without_buys")
    if idle_hours >= idle_threshold_hours:
        reasons.append(f"idle_hours>={idle_threshold_hours:g}")
    if decisions == 0 and idle_hours >= idle_threshold_hours:
        reasons.append("no_recent_decisions")

    return IdleTrigger(
        active=idle_hours >= idle_threshold_hours,
        idle_hours=idle_hours,
        reasons=sorted(set(reasons)),
    )


def select_research_spaces(
    report_bundle: dict[str, Any],
    scoreboard_entries: list[dict[str, Any]],
    *,
    config: AutoResearchConfig,
    seed: int | None = None,
) -> SpaceSelection:
    idle = detect_idle_trigger(report_bundle, idle_threshold_hours=config.idle_threshold_hours)
    space_count = max(1, min(config.max_parallel, config.max_candidates_per_cycle))
    if config.space:
        return SpaceSelection(spaces=[config.space], idle_trigger=idle, mode="override")
    if idle.active:
        selected = list(IDLE_FOCUS_SPACES[:space_count])
        return SpaceSelection(spaces=selected, idle_trigger=idle, mode="idle_focus")
    suggestion = suggest_spaces(scoreboard_entries, n=space_count, seed=seed)
    return SpaceSelection(spaces=suggestion.spaces, idle_trigger=idle, mode=suggestion.mode)


def _cycle_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("autoresearch_%Y%m%d_%H%M%S")


def _cycle_report_path(root: Path, cycle_id: str) -> Path:
    return research_runs_dir(root) / "logs" / f"{cycle_id}.json"


def _batch_results(batch: Any) -> list[Any]:
    results = getattr(batch, "results", None)
    if isinstance(results, list):
        return results
    if isinstance(batch, dict) and isinstance(batch.get("results"), list):
        return batch["results"]
    return []


def _result_field(result: Any, key: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def _best_accepted_replay(batch_results: list[Any]) -> Any | None:
    accepted = [result for result in batch_results if _result_field(result, "status") == STATUS_ACCEPTED_REPLAY]
    if not accepted:
        return None
    return max(accepted, key=lambda result: float(_result_field(result, "objective_score", 0.0) or 0.0))


def _candidate_policy_path(root: Path, run_id: str | None) -> Path | None:
    if not run_id:
        return None
    path = research_runs_dir(root) / "runs" / str(run_id) / "candidate_policy.json"
    return path if path.exists() else None


def _candidates_for_spaces(total: int, spaces: list[str]) -> dict[str, int]:
    if not spaces:
        return {}
    base = max(1, total // len(spaces))
    remaining = max(0, total - base * len(spaces))
    counts: dict[str, int] = {}
    for index, space in enumerate(spaces):
        counts[space] = base + (1 if index < remaining else 0)
    return counts


def _regenerate_reports(root: Path, regenerate_func: Callable[[Path], dict[str, Any]] | None) -> dict[str, Any]:
    if regenerate_func is not None:
        return regenerate_func(root)
    from analytics.core_report_scheduler import regenerate_core_reports

    return regenerate_core_reports(root, include_test_events=False)


def run_autoresearch_cycle(
    *,
    root: str | Path | None = None,
    config: AutoResearchConfig | Mapping[str, Any] | None = None,
    seed: int | None = None,
    batch_runner_func: Callable[..., Any] | None = None,
    paper_start_func: Callable[..., Any] | None = None,
    regenerate_func: Callable[[Path], dict[str, Any]] | None = None,
) -> AutoResearchCycleResult:
    resolved_root = project_root(root)
    resolved_config = config if isinstance(config, AutoResearchConfig) else load_scheduler_config(overrides=config)
    cycle = _cycle_id()
    warnings: list[str] = []
    failures: list[str] = []
    report_bundle_path = research_runs_dir(resolved_root) / "report_bundle_latest.json"

    if not resolved_config.enabled:
        result = AutoResearchCycleResult(
            cycle_id=cycle,
            status="disabled",
            config=resolved_config,
            selected_spaces=[],
            idle_trigger=IdleTrigger(active=False, idle_hours=0.0),
            report_bundle_path=None,
            cycle_report_path=_cycle_report_path(resolved_root, cycle),
        )
        _write_cycle_report(resolved_root, result)
        return result

    if resolved_config.max_parallel > 1:
        warnings.append("max_parallel_is_executed_sequentially")

    demotion = None
    if resolved_config.profitability_demotion_enabled:
        demotion = evaluate_paper_profitability_for_demotion(root=resolved_root)

    if resolved_config.regenerate_reports:
        try:
            _regenerate_reports(resolved_root, regenerate_func)
        except Exception as exc:
            warnings.append(f"regenerate_reports_failed:{exc}")

    bundle = build_report_bundle(resolved_root, write=True, include_api_budget=resolved_config.api_budget_aware)
    scoreboard = load_scoreboard(resolved_root)
    selection = select_research_spaces(bundle, scoreboard, config=resolved_config, seed=seed)
    counts = _candidates_for_spaces(resolved_config.max_candidates_per_cycle, selection.spaces)
    batch_func = batch_runner_func or run_research_batch
    batches: list[Any] = []

    for index, space in enumerate(selection.spaces):
        try:
            batch = batch_func(
                space_name=space,
                n=counts.get(space, 1),
                seed=None if seed is None else seed + index,
                mode=resolved_config.batch_mode,
                root=resolved_root,
                batch_id=f"{cycle}_{space}",
                regenerate_baseline=resolved_config.regenerate_reports,
                regenerate_replay=True,
                regenerate_func=regenerate_func,
            )
            batches.append(batch)
        except Exception as exc:
            failures.append(f"batch_failed:{space}:{exc}")

    paper_forward_start: dict[str, Any] | None = None
    if resolved_config.auto_paper_promote:
        all_results = [result for batch in batches for result in _batch_results(batch)]
        best = _best_accepted_replay(all_results)
        candidate_path = _candidate_policy_path(resolved_root, str(_result_field(best, "run_id") or "")) if best else None
        if candidate_path is not None:
            starter = paper_start_func or start_paper_forward
            paper = starter(
                candidate_path,
                root=resolved_root,
                run_id=f"paper_{_result_field(best, 'run_id')}",
                profile_id=str(_result_field(best, "run_id") or _result_field(best, "proposal_id") or "cycle_candidate"),
            )
            paper_forward_start = _as_dict(paper)

    status = "failed" if failures else "completed"
    result = AutoResearchCycleResult(
        cycle_id=cycle,
        status=status,
        config=resolved_config,
        selected_spaces=selection.spaces,
        idle_trigger=selection.idle_trigger,
        report_bundle_path=report_bundle_path,
        batches=batches,
        paper_forward_start=paper_forward_start,
        demotion=demotion,
        warnings=warnings,
        failures=failures,
        cycle_report_path=_cycle_report_path(resolved_root, cycle),
    )
    _write_cycle_report(resolved_root, result)
    return result


def _write_cycle_report(root: Path, result: AutoResearchCycleResult) -> None:
    logs_dir = research_runs_dir(root) / "logs"
    report_path = result.cycle_report_path or logs_dir / f"{result.cycle_id}.json"
    payload = result.as_dict()
    payload["cycle_report_path"] = str(report_path)
    _write_json(report_path, payload)
    _write_json(logs_dir / "autoresearch_cycle_latest.json", payload)


def _latest_paper_run(root: Path) -> Path | None:
    paper_root = research_runs_dir(root) / "paper_forward"
    if not paper_root.exists():
        return None
    candidates: list[tuple[str, Path]] = []
    for run_dir in paper_root.iterdir():
        if not run_dir.is_dir():
            continue
        state = _read_json(run_dir / "paper_forward_state.json")
        if not isinstance(state, dict):
            continue
        if str(state.get("status") or "") not in ACTIVE_PAPER_STATUSES:
            continue
        stamp = str(state.get("started_at_utc") or run_dir.stat().st_mtime)
        candidates.append((stamp, run_dir))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]


def _paper_metrics_from_reports(root: Path) -> dict[str, Any]:
    summary = _read_json(root / "data" / "metrics" / "current_run_summary.json")
    diagnostics = _read_json(root / "data" / "metrics" / "current_run_trade_diagnostics.json")
    payload: dict[str, Any] = {}
    for item in (summary, diagnostics):
        if isinstance(item, dict):
            payload.update(item)
    return payload


def evaluate_paper_profitability_for_demotion(
    *,
    root: str | Path | None = None,
    run_id_or_dir: str | Path | None = None,
    paper_metrics: dict[str, Any] | None = None,
    baseline_metrics: dict[str, Any] | None = None,
    min_median_delta: float = -3.0,
    rollback_on_degrade: bool = True,
) -> PaperDemotionResult:
    resolved_root = project_root(root)
    run_dir = Path(run_id_or_dir) if run_id_or_dir is not None else _latest_paper_run(resolved_root)
    if run_dir is None:
        result = PaperDemotionResult(
            checked=False,
            run_id=None,
            status="no_active_paper",
            demotion_report_path=research_runs_dir(resolved_root) / "paper_forward" / "demotion_latest.json",
        )
        _write_demotion_latest(resolved_root, result)
        return result
    if not run_dir.is_absolute() and len(run_dir.parts) == 1:
        run_dir = research_runs_dir(resolved_root) / "paper_forward" / str(run_dir)

    state = _read_json(run_dir / "paper_forward_state.json")
    if not isinstance(state, dict):
        result = PaperDemotionResult(
            checked=False,
            run_id=run_dir.name,
            status="missing_paper_state",
            warnings=[f"missing_paper_forward_state:{run_dir}"],
            demotion_report_path=run_dir / "demotion_report.json",
        )
        _write_demotion_reports(resolved_root, run_dir, result)
        return result

    resolved_baseline = dict(baseline_metrics) if baseline_metrics is not None else {}
    if not resolved_baseline:
        payload = _read_json(run_dir / "baseline_metrics.json")
        if isinstance(payload, dict):
            resolved_baseline = payload
    resolved_paper = dict(paper_metrics) if paper_metrics is not None else _paper_metrics_from_reports(resolved_root)

    if not resolved_baseline or not resolved_paper:
        result = PaperDemotionResult(
            checked=True,
            run_id=str(state.get("run_id") or run_dir.name),
            status="insufficient_data",
            warnings=["missing_baseline_or_paper_metrics"],
            demotion_report_path=run_dir / "demotion_report.json",
        )
        _write_demotion_reports(resolved_root, run_dir, result)
        return result

    baseline_for_objective = dict(resolved_baseline)
    paper_for_objective = dict(resolved_paper)
    objective = calculate_objective_score(baseline_for_objective, paper_for_objective)
    deltas = objective.metric_deltas
    reasons: list[str] = []
    median_delta = float(deltas.get("median_pnl_pct") or 0.0)
    severe_delta = float(deltas.get("severe_loss_count") or 0.0)
    liquidity_delta = float(deltas.get("liquidity_crush_count") or 0.0)
    objective_score_delta = _metric_float(paper_for_objective, "objective_score") - _metric_float(
        baseline_for_objective,
        "objective_score",
    )
    if median_delta < min_median_delta:
        reasons.append(f"median_pnl_delta<{min_median_delta:g}")
    if severe_delta > 0:
        reasons.append("severe_loss_count_delta>0")
    if liquidity_delta > 0:
        reasons.append("liquidity_crush_count_delta>0")
    if "objective_score" in paper_for_objective and "objective_score" in baseline_for_objective and objective_score_delta < 0:
        reasons.append("objective_score_degraded")
    elif objective.score < 0:
        reasons.append("objective_score_degraded")
    reasons.extend(reason for reason in objective.rejection_reasons if reason not in reasons)

    rollback = None
    status = "healthy"
    if reasons:
        status = STATUS_REJECTED_PAPER
        if rollback_on_degrade:
            rollback = rollback_paper_candidate(run_dir, root=resolved_root, reason="profitability_degraded")
        _record_demotion_evaluation(resolved_root, run_dir, state, objective, reasons)

    result = PaperDemotionResult(
        checked=True,
        run_id=str(state.get("run_id") or run_dir.name),
        status=status,
        degraded=bool(reasons),
        rejection_reasons=sorted(set(reasons)),
        objective=objective,
        rollback=rollback,
        demotion_report_path=run_dir / "demotion_report.json",
    )
    _write_demotion_reports(resolved_root, run_dir, result)
    return result


def _record_demotion_evaluation(
    root: Path,
    run_dir: Path,
    state: dict[str, Any],
    objective: ObjectiveResult,
    reasons: list[str],
) -> None:
    candidate_policy = _read_json(run_dir / "candidate_policy.json")
    if not isinstance(candidate_policy, dict):
        return
    record_evaluation(
        run_id=str(state.get("run_id") or run_dir.name),
        candidate_policy=candidate_policy,
        evaluation_result=EvaluationResult(
            status=STATUS_REJECTED_PAPER,
            accepted=False,
            objective=objective,
            rejection_reasons=reasons,
            run_id=str(state.get("run_id") or run_dir.name),
            proposal_id=str(candidate_policy.get("proposal_id") or ""),
        ),
        root=root,
    )


def _write_demotion_latest(root: Path, result: PaperDemotionResult) -> None:
    _write_json(research_runs_dir(root) / "paper_forward" / "demotion_latest.json", result.as_dict())


def _write_demotion_reports(root: Path, run_dir: Path, result: PaperDemotionResult) -> None:
    report_path = run_dir / "demotion_report.json"
    payload = result.as_dict()
    payload["demotion_report_path"] = str(report_path)
    _write_json(report_path, payload)
    _write_json(research_runs_dir(root) / "paper_forward" / "demotion_latest.json", payload)


def run_autoresearch_loop(
    *,
    root: str | Path | None = None,
    config: AutoResearchConfig | Mapping[str, Any] | None = None,
    seed: int | None = None,
    once: bool = True,
) -> list[AutoResearchCycleResult]:
    resolved_config = config if isinstance(config, AutoResearchConfig) else load_scheduler_config(overrides=config)
    results: list[AutoResearchCycleResult] = []
    while True:
        result = run_autoresearch_cycle(root=root, config=resolved_config, seed=seed)
        results.append(result)
        if once:
            return results
        time.sleep(resolved_config.interval_hours * 3600.0)


__all__ = [
    "ACTIVE_PAPER_STATUSES",
    "AUTORESEARCH_CONFIG_DEFAULTS",
    "AutoResearchConfig",
    "AutoResearchCycleResult",
    "AutoResearchSchedulerError",
    "IDLE_FOCUS_SPACES",
    "IdleTrigger",
    "PaperDemotionResult",
    "SpaceSelection",
    "detect_idle_trigger",
    "evaluate_paper_profitability_for_demotion",
    "load_scheduler_config",
    "run_autoresearch_cycle",
    "run_autoresearch_loop",
    "select_research_spaces",
    "validate_scheduler_config",
]
