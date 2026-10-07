from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from research_loop.bandit import DEFAULT_SPACES, suggest_spaces
from research_loop.candidate_generator import applicable_generation_spaces
from research_loop.batch_runner import BatchRunResult, run_research_batch
from research_loop.evaluator import EvaluationResult, STATUS_ACCEPTED_REPLAY, STATUS_NEEDS_PAPER
from research_loop.objectives import ObjectiveResult, calculate_objective_score
from research_loop.paper_forward import STATUS_ACCEPTED_PAPER, STATUS_PAPER_FORWARD_STARTED, STATUS_REJECTED_PAPER, start_paper_forward, _load_current_paper_metrics
from research_loop.paths import project_root, research_runs_dir
from research_loop.policy_promoter import PromotionResult, promote_to_paper_candidate
from research_loop.report_bundle import build_report_bundle
from research_loop.rollback import RollbackResult, rollback_paper_candidate
from research_loop.runtime_state import (
    EVENT_AUTORESEARCH_API_BUDGET_READY,
    EVENT_AUTORESEARCH_CANDIDATES_GENERATED,
    EVENT_AUTORESEARCH_CANDIDATE_ACCEPTED,
    EVENT_AUTORESEARCH_CANDIDATE_FAILED,
    EVENT_AUTORESEARCH_CANDIDATE_REJECTED,
    EVENT_AUTORESEARCH_CYCLE_END,
    EVENT_AUTORESEARCH_CYCLE_START,
    EVENT_AUTORESEARCH_ERROR,
    EVENT_AUTORESEARCH_PAPER_PROMOTION_CREATED,
    EVENT_AUTORESEARCH_PAPER_PROMOTION_SKIPPED,
    EVENT_AUTORESEARCH_REPLAY_DONE,
    EVENT_AUTORESEARCH_REPLAY_START,
    EVENT_AUTORESEARCH_REPORT_BUNDLE_READY,
    EVENT_AUTORESEARCH_SCOREBOARD_UPDATED,
    append_event,
)
from research_loop.scoreboard import load_scoreboard, record_evaluation, upsert_scoreboard_entry

IDLE_FOCUS_SPACES = ("paper_bootstrap", "paper_exploration", "moonshot_micro", "shadow_followup_micro")
ACTIVE_PAPER_STATUSES = {STATUS_PAPER_FORWARD_STARTED, STATUS_ACCEPTED_PAPER}
SKIPPED_RECENT_CYCLE = "skipped_recent_cycle"
SKIPPED_NO_DATA = "skipped_no_data"
RECENT_CYCLE_FRACTION = 0.90
MIN_RECENT_CYCLE_SECONDS = 60.0
AUTORESEARCH_CONFIG_DEFAULTS = {
    "enabled": True,
    "mode": "paper_replay",
    "interval_hours": 1.0,
    "max_candidates_per_cycle": 25,
    "max_parallel": 1,
    "api_budget_aware": True,
    "live_promotion_enabled": False,
    "auto_paper_promote": False,
    "auto_live_promote": False,
    "idle_threshold_hours": 3.0,
    "regenerate_reports": False,
    "batch_mode": "seeded_random",
    "space": None,
    "profitability_demotion_enabled": True,
    "force_cycle": False,
}


class AutoResearchSchedulerError(RuntimeError):
    pass


@dataclass(frozen=True)
class AutoResearchConfig:
    enabled: bool = True
    mode: str = "paper_replay"
    interval_hours: float = 1.0
    max_candidates_per_cycle: int = 25
    max_parallel: int = 1
    api_budget_aware: bool = True
    live_promotion_enabled: bool = False
    auto_paper_promote: bool = False
    auto_live_promote: bool = False
    idle_threshold_hours: float = 3.0
    regenerate_reports: bool = False
    batch_mode: str = "seeded_random"
    space: str | None = None
    profitability_demotion_enabled: bool = True
    force_cycle: bool = False

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
            "force_cycle": self.force_cycle,
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
    paper_candidate: dict[str, Any] | None = None
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
            "paper_candidate": self.paper_candidate,
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
        "force_cycle": _bool_value(
            source.get("AUTORESEARCH_FORCE_CYCLE"),
            bool(defaults["force_cycle"]),
        ),
    }
    if overrides:
        values.update(dict(overrides))
    config = AutoResearchConfig(
        enabled=_bool_value(values["enabled"], True),
        mode=str(values["mode"]),
        interval_hours=max(0.01, _float_value(values["interval_hours"], 1.0)),
        max_candidates_per_cycle=max(1, _int_value(values["max_candidates_per_cycle"], 25)),
        max_parallel=max(1, _int_value(values["max_parallel"], 1)),
        api_budget_aware=_bool_value(values["api_budget_aware"], True),
        live_promotion_enabled=_bool_value(values["live_promotion_enabled"], False),
        auto_paper_promote=_bool_value(values["auto_paper_promote"], False),
        auto_live_promote=_bool_value(values["auto_live_promote"], False),
        idle_threshold_hours=max(0.0, _float_value(values["idle_threshold_hours"], 3.0)),
        regenerate_reports=_bool_value(values["regenerate_reports"], False),
        batch_mode=str(values["batch_mode"]),
        space=str(values["space"]) if values.get("space") else None,
        profitability_demotion_enabled=_bool_value(values["profitability_demotion_enabled"], True),
        force_cycle=_bool_value(values["force_cycle"], False),
    )
    validate_scheduler_config(config)
    return config


def validate_scheduler_config(config: AutoResearchConfig) -> None:
    errors: list[str] = []
    if config.live_promotion_enabled:
        errors.append("AUTORESEARCH_LIVE_PROMOTION_ENABLED_must_be_false")
    if config.auto_paper_promote:
        errors.append("AUTORESEARCH_AUTO_PAPER_PROMOTE_must_be_false")
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


def _prepend_unique(values: list[str], preferred: list[str]) -> list[str]:
    out: list[str] = []
    for value in preferred + values:
        if value and value not in out:
            out.append(value)
    return out


def moonshot_pressure_reasons(report_bundle: dict[str, Any]) -> list[str]:
    moonshot = _nested_dict(report_bundle, "moonshots", "moonshot_micro_lottery")
    current_missed_summary = _nested_dict(report_bundle, "current_run", "missed_pumps", "summary")
    historical_missed = _nested_dict(report_bundle, "historical", "missed_pumps")
    historical_missed_summary = historical_missed.get("summary") if isinstance(historical_missed.get("summary"), dict) else {}
    reasons: list[str] = []
    missed_peak500 = max(
        _metric_int(current_missed_summary, "peak_500"),
        _metric_int(historical_missed, "missed_peak500_count"),
        _metric_int(historical_missed_summary, "peak_500"),
    )
    missed_peak1000 = max(
        _metric_int(current_missed_summary, "peak_1000"),
        _metric_int(historical_missed, "missed_peak1000_count"),
        _metric_int(historical_missed_summary, "peak_1000"),
    )
    extreme_candidates = _metric_int(moonshot, "extreme_cluster_candidates")
    cluster_tail_candidates = _metric_int(moonshot, "cluster_tail_candidates")
    buys = _metric_int(moonshot, "buys", "paper_buys", "confirmed_moonshot_buy")
    if missed_peak1000 > 0:
        reasons.append("missed_peak1000")
    if missed_peak500 > 0:
        reasons.append("missed_peak500")
    if extreme_candidates > 0 and buys == 0:
        reasons.append("extreme_cluster_candidates_without_buys")
    if cluster_tail_candidates > 0 and buys == 0:
        reasons.append("cluster_tail_candidates_without_buys")
    return sorted(set(reasons))


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
        if not applicable_generation_spaces((config.space,)):
            raise AutoResearchSchedulerError(f"search_space_inapplicable_to_exact_paper_size:{config.space}")
        return SpaceSelection(spaces=[config.space], idle_trigger=idle, mode="override")
    available_spaces = applicable_generation_spaces(DEFAULT_SPACES)
    moonshot_reasons = moonshot_pressure_reasons(report_bundle)
    if moonshot_reasons:
        preferred = ["moonshot_micro", "shadow_followup_micro", "runner_exit"]
        if idle.active:
            selected = _prepend_unique(list(IDLE_FOCUS_SPACES), preferred)[:space_count]
            return SpaceSelection(spaces=selected, idle_trigger=idle, mode="idle_moonshot_pressure")
        suggestion = suggest_spaces(scoreboard_entries, n=space_count, seed=seed, spaces=available_spaces)
        selected = _prepend_unique(suggestion.spaces, preferred)[:space_count]
        return SpaceSelection(spaces=selected, idle_trigger=idle, mode="moonshot_pressure")
    if idle.active:
        selected = list(IDLE_FOCUS_SPACES[:space_count])
        return SpaceSelection(spaces=selected, idle_trigger=idle, mode="idle_focus")
    suggestion = suggest_spaces(scoreboard_entries, n=space_count, seed=seed, spaces=available_spaces)
    return SpaceSelection(spaces=suggestion.spaces, idle_trigger=idle, mode=suggestion.mode)


def _cycle_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("autoresearch_%Y%m%d_%H%M%S")


def _cycle_seed(cycle_id: str) -> int:
    digest = hashlib.sha256(cycle_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _cycle_report_path(root: Path, cycle_id: str) -> Path:
    return research_runs_dir(root) / "logs" / f"{cycle_id}.json"


def _parse_dt(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _file_mtime(path: Path) -> dt.datetime | None:
    if not path.exists():
        return None
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    except Exception:
        return None


def _latest_completed_cycle_time(root: Path) -> dt.datetime | None:
    latest_path = research_runs_dir(root) / "logs" / "autoresearch_cycle_latest.json"
    payload = _read_json(latest_path)
    status = str(payload.get("status") or "") if isinstance(payload, dict) else ""
    if status and status != "failed":
        return _file_mtime(latest_path)
    return None


def _recent_cycle_warning(root: Path, config: AutoResearchConfig) -> str | None:
    if config.force_cycle:
        return None
    if config.space:
        return None
    latest_at = _latest_completed_cycle_time(root)
    if latest_at is None:
        return None
    interval_s = max(MIN_RECENT_CYCLE_SECONDS, float(config.interval_hours or 0.0) * 3600.0 * RECENT_CYCLE_FRACTION)
    elapsed_s = max(0.0, (dt.datetime.now(dt.timezone.utc) - latest_at).total_seconds())
    if elapsed_s >= interval_s:
        return None
    return f"recent_cycle_elapsed_s={elapsed_s:.0f}<min_s={interval_s:.0f}"


def _file_has_data(path: Path, *, min_bytes: int = 4) -> bool:
    try:
        return path.exists() and path.stat().st_size > min_bytes
    except Exception:
        return False


def _sqlite_position_count(root: Path) -> int:
    db_path = root / "data" / "memebotdatabase.db"
    if not db_path.exists():
        return 0
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute("SELECT COUNT(*) FROM positions").fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def _report_has_signal(path: Path) -> bool:
    if not _file_has_data(path):
        return False
    if path.stat().st_size > 5 * 1024 * 1024:
        return True
    payload = _read_json(path)
    if isinstance(payload, list):
        return bool(payload)
    if not isinstance(payload, dict) or payload.get("placeholder"):
        return False
    for key in (
        "rows",
        "closed_trades",
        "closed_positions",
        "buys",
        "actual_paper_buys",
        "candidates_seen",
        "strategy_decisions",
    ):
        try:
            value = payload.get(key)
            if isinstance(value, list) and value:
                return True
            if float(value or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return any(
        _metric_int(summary, key) > 0
        for key in ("rows", "closed", "closed_trades", "closed_positions", "buys", "missed", "peak_100", "peak_500")
    )


def _research_data_warnings(root: Path, config: AutoResearchConfig) -> list[str]:
    if config.space:
        return []
    metrics = root / "data" / "metrics"
    data = root / "data"
    if _file_has_data(metrics / "candidate_outcomes.jsonl"):
        return []
    if _file_has_data(metrics / "runtime_events.jsonl", min_bytes=1024):
        return []
    if _sqlite_position_count(root) > 0:
        return []
    if _report_has_signal(data / "paper_portfolio.json"):
        return []
    for name in (
        "current_run_summary.json",
        "current_run_trade_diagnostics.json",
        "paper_real_outcomes.json",
        "moonshot_micro_lottery_report.json",
        "current_run_missed_pumps.json",
    ):
        if _report_has_signal(metrics / name):
            return []
    return ["autoresearch_no_actionable_data"]


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


def _batch_health(batch: Any, *, planned_candidates: int = 0) -> dict[str, int | str]:
    results = _batch_results(batch)
    derived_completed = 0
    derived_skipped = 0
    derived_failed = 0
    for result in results:
        if bool(_result_field(result, "skipped", False)):
            derived_skipped += 1
        elif str(_result_field(result, "status") or "") == "failed":
            derived_failed += 1
        else:
            derived_completed += 1

    completed = max(derived_completed, _int_value(_result_field(batch, "completed"), derived_completed))
    skipped = max(derived_skipped, _int_value(_result_field(batch, "skipped"), derived_skipped))
    failed = max(derived_failed, _int_value(_result_field(batch, "failed"), derived_failed))
    generated = max(
        len(results),
        completed + skipped + failed,
        _int_value(_result_field(batch, "candidates_generated"), max(len(results), planned_candidates)),
    )
    status = "completed"
    if failed > 0:
        status = "degraded" if completed > 0 else "failed"
    return {
        "status": status,
        "generated": generated,
        "completed": completed,
        "skipped": skipped,
        "failed": failed,
    }


def _batch_failure_message(space: str, batch_id: str, health: Mapping[str, int | str]) -> str:
    return (
        f"batch_candidate_failures:{space}:{batch_id}:"
        f"failed={health['failed']}:completed={health['completed']}:skipped={health['skipped']}"
    )


def _result_event_replay_used(root: Path, result: Any) -> bool:
    run_id = str(_result_field(result, "run_id") or "")
    if not run_id:
        return False
    metrics = _read_json(research_runs_dir(root) / "runs" / run_id / "replay_metrics.json")
    if not isinstance(metrics, dict):
        return False
    value = metrics.get("event_replay_used_for_acceptance")
    if value is True:
        return True
    if str(value or "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    for key in ("current_run_metrics", "historical_metrics", "combined_metrics"):
        nested = metrics.get(key)
        if not isinstance(nested, dict):
            continue
        nested_value = nested.get("event_replay_used_for_acceptance")
        if nested_value is True or str(nested_value or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def _best_accepted_replay(batch_results: list[Any], *, root: Path | None = None) -> Any | None:
    accepted = [result for result in batch_results if _result_field(result, "status") == STATUS_ACCEPTED_REPLAY]
    if root is not None:
        accepted = [result for result in accepted if _result_event_replay_used(root, result)]
    if not accepted:
        return None
    return max(accepted, key=lambda result: float(_result_field(result, "objective_score", 0.0) or 0.0))


def _candidate_policy_path(root: Path, run_id: str | None) -> Path | None:
    if not run_id:
        return None
    path = research_runs_dir(root) / "runs" / str(run_id) / "candidate_policy.json"
    return path if path.exists() else None


def _scoreboard_entry_for_result(
    root: Path,
    result: Any,
    candidate_policy: dict[str, Any],
) -> dict[str, Any]:
    run_id = str(_result_field(result, "run_id") or "")
    proposal_id = str(_result_field(result, "proposal_id") or candidate_policy.get("proposal_id") or "")
    entry = _result_field(result, "scoreboard_entry")
    if isinstance(entry, dict):
        return dict(entry)
    for existing in load_scoreboard(root):
        if str(existing.get("run_id") or "") == run_id and str(existing.get("proposal_id") or "") == proposal_id:
            return dict(existing)
    return {
        "run_id": run_id,
        "proposal_id": proposal_id,
        "status": STATUS_ACCEPTED_REPLAY,
        "optimized_metric": str(candidate_policy.get("optimized_metric") or ""),
        "optimization_scope": str(candidate_policy.get("optimization_scope") or "combined"),
        "objective_score": _result_field(result, "objective_score"),
        "total_pnl_delta": 0.0,
        "avg_pnl_delta": 0.0,
        "median_pnl_delta": 0.0,
        "win_rate_delta": 0.0,
        "runner_capture_delta": 0.0,
        "moonshot_capture_delta": 0.0,
        "severe_loss_delta": 0.0,
        "liquidity_crush_delta": 0.0,
        "adverse_tick_delta": 0.0,
        "api_budget_delta": {
            "api_429_count_delta": 0.0,
            "provider_degraded_minutes_delta": 0.0,
            "gecko_429_count_delta": 0.0,
            "birdeye_429_count_delta": 0.0,
            "jupiter_rate_limit_count_delta": 0.0,
        },
        "created_at_utc": str(candidate_policy.get("created_at_utc") or ""),
        "evaluated_at_utc": utc_now(),
        "rejection_reasons": [],
        "warnings": [],
    }


def _append_unique(values: list[Any], *items: str) -> list[str]:
    updated = [str(value) for value in values]
    for item in items:
        if item not in updated:
            updated.append(item)
    return updated


def _mark_scoreboard_needs_paper(
    root: Path,
    result: Any,
    candidate_policy: dict[str, Any],
    promotion: PromotionResult,
) -> dict[str, Any]:
    entry = _scoreboard_entry_for_result(root, result, candidate_policy)
    accepted_evaluated_at = str(entry.get("evaluated_at_utc") or "")
    entry.update(
        {
            "status": STATUS_NEEDS_PAPER,
            "needs_paper": True,
            "accepted_replay_status": STATUS_ACCEPTED_REPLAY,
            "accepted_replay_evaluated_at_utc": accepted_evaluated_at,
            "paper_profile": promotion.profile_name,
            "paper_profile_path": str(promotion.profile_path),
            "paper_candidate_exported_at_utc": utc_now(),
            "evaluated_at_utc": utc_now(),
        }
    )
    warnings = entry.get("warnings") if isinstance(entry.get("warnings"), list) else []
    entry["warnings"] = _append_unique(warnings, "paper_validation_required")
    upsert_scoreboard_entry(entry, root=root)
    return entry


def _paper_candidate_payload(
    *,
    result: Any,
    candidate_path: Path,
    promotion: PromotionResult,
    scoreboard_entry: dict[str, Any],
    auto_paper_promote: bool,
) -> dict[str, Any]:
    return {
        "status": STATUS_NEEDS_PAPER,
        "source_status": STATUS_ACCEPTED_REPLAY,
        "run_id": str(_result_field(result, "run_id") or ""),
        "proposal_id": str(_result_field(result, "proposal_id") or promotion.proposal_id),
        "candidate_policy_path": str(candidate_path),
        "promotion": promotion.as_dict(),
        "scoreboard_entry": dict(scoreboard_entry),
        "auto_paper_promote": auto_paper_promote,
    }


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


def _runtime_event(root: Path, event_type: str, **payload: Any) -> None:
    append_event(root, event_type, payload)


def _metric_payload_int(payload: dict[str, Any], key: str) -> int:
    try:
        return int(float(payload.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _candidate_event_payload(
    result: Any,
    *,
    cycle_id: str,
    space: str,
    batch_id: str,
) -> dict[str, Any]:
    scoreboard_entry = _result_field(result, "scoreboard_entry")
    payload = {
        "cycle_id": cycle_id,
        "space": space,
        "batch_id": batch_id,
        "proposal_id": _result_field(result, "proposal_id"),
        "run_id": _result_field(result, "run_id"),
        "status": _result_field(result, "status"),
        "objective_score": _result_field(result, "objective_score"),
        "error": _result_field(result, "error"),
    }
    if isinstance(scoreboard_entry, dict):
        payload["rejection_reasons"] = list(scoreboard_entry.get("rejection_reasons") or [])
        payload["warnings"] = list(scoreboard_entry.get("warnings") or [])
    duplicate_reasons = _result_field(result, "duplicate_reasons")
    if duplicate_reasons:
        payload["duplicate_reasons"] = list(duplicate_reasons)
    return payload


def _emit_candidate_result_events(root: Path, cycle_id: str, space: str, batch: Any) -> None:
    batch_id = str(_result_field(batch, "batch_id") or "")
    for result in _batch_results(batch):
        status = str(_result_field(result, "status") or "")
        payload = _candidate_event_payload(result, cycle_id=cycle_id, space=space, batch_id=batch_id)
        if status == STATUS_ACCEPTED_REPLAY:
            _runtime_event(root, EVENT_AUTORESEARCH_CANDIDATE_ACCEPTED, **payload)
        elif status == "rejected":
            _runtime_event(root, EVENT_AUTORESEARCH_CANDIDATE_REJECTED, **payload)
        elif status == "failed":
            _runtime_event(root, EVENT_AUTORESEARCH_CANDIDATE_FAILED, **payload)


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
    validate_scheduler_config(resolved_config)
    cycle = _cycle_id()
    effective_seed = int(seed) if seed is not None else _cycle_seed(cycle)
    warnings: list[str] = []
    failures: list[str] = []
    report_bundle_path = research_runs_dir(resolved_root) / "report_bundle_latest.json"
    _runtime_event(
        resolved_root,
        EVENT_AUTORESEARCH_CYCLE_START,
        cycle_id=cycle,
        seed=seed,
        effective_seed=effective_seed,
        config=resolved_config.as_dict(),
    )

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
        _runtime_event(resolved_root, EVENT_AUTORESEARCH_CYCLE_END, cycle_id=cycle, status="disabled")
        return result

    if resolved_config.max_parallel > 1:
        warnings.append("max_parallel_is_executed_sequentially")

    recent_warning = _recent_cycle_warning(resolved_root, resolved_config)
    if recent_warning:
        warnings.append(recent_warning)
        result = AutoResearchCycleResult(
            cycle_id=cycle,
            status=SKIPPED_RECENT_CYCLE,
            config=resolved_config,
            selected_spaces=[],
            idle_trigger=IdleTrigger(active=False, idle_hours=0.0),
            report_bundle_path=report_bundle_path,
            warnings=warnings,
            cycle_report_path=_cycle_report_path(resolved_root, cycle),
        )
        _write_cycle_report(resolved_root, result)
        _runtime_event(
            resolved_root,
            EVENT_AUTORESEARCH_CYCLE_END,
            cycle_id=cycle,
            status=SKIPPED_RECENT_CYCLE,
            failures=[],
            warnings=list(warnings),
        )
        return result

    data_warnings = _research_data_warnings(resolved_root, resolved_config)
    if data_warnings:
        warnings.extend(data_warnings)
        result = AutoResearchCycleResult(
            cycle_id=cycle,
            status=SKIPPED_NO_DATA,
            config=resolved_config,
            selected_spaces=[],
            idle_trigger=IdleTrigger(active=False, idle_hours=0.0),
            report_bundle_path=report_bundle_path,
            warnings=warnings,
            cycle_report_path=_cycle_report_path(resolved_root, cycle),
        )
        _write_cycle_report(resolved_root, result)
        _runtime_event(
            resolved_root,
            EVENT_AUTORESEARCH_CYCLE_END,
            cycle_id=cycle,
            status=SKIPPED_NO_DATA,
            failures=[],
            warnings=list(warnings),
        )
        return result

    demotion = None
    if resolved_config.profitability_demotion_enabled:
        demotion = evaluate_paper_profitability_for_demotion(root=resolved_root)

    if resolved_config.regenerate_reports:
        try:
            _regenerate_reports(resolved_root, regenerate_func)
        except Exception as exc:
            warnings.append(f"regenerate_reports_failed:{exc}")

    try:
        bundle = build_report_bundle(resolved_root, write=True, include_api_budget=resolved_config.api_budget_aware)
    except Exception as exc:
        _runtime_event(
            resolved_root,
            EVENT_AUTORESEARCH_ERROR,
            cycle_id=cycle,
            phase="report_bundle",
            error=str(exc),
        )
        raise
    _runtime_event(
        resolved_root,
        EVENT_AUTORESEARCH_REPORT_BUNDLE_READY,
        cycle_id=cycle,
        report_bundle_path=str(report_bundle_path),
        include_api_budget=resolved_config.api_budget_aware,
        generated_at_utc=bundle.get("generated_at_utc"),
    )
    api_budget = bundle.get("api_budget") if isinstance(bundle.get("api_budget"), dict) else {}
    _runtime_event(
        resolved_root,
        EVENT_AUTORESEARCH_API_BUDGET_READY,
        cycle_id=cycle,
        api_budget_path=str(research_runs_dir(resolved_root) / "api_budget.json"),
        api_429_count=(
            _metric_payload_int(api_budget, "gecko_429_count")
            + _metric_payload_int(api_budget, "birdeye_429_count")
            + _metric_payload_int(api_budget, "jupiter_rate_limit_count")
        ),
        provider_degraded_minutes=_metric_payload_int(api_budget, "provider_degraded_minutes"),
        cooldown_count=_metric_payload_int(api_budget, "cooldown_count"),
        rpc_errors=_metric_payload_int(api_budget, "rpc_errors"),
    )
    scoreboard = load_scoreboard(resolved_root)
    selection = select_research_spaces(bundle, scoreboard, config=resolved_config, seed=effective_seed)
    counts = _candidates_for_spaces(resolved_config.max_candidates_per_cycle, selection.spaces)
    batch_func = batch_runner_func or run_research_batch
    batches: list[Any] = []
    completed_candidates = 0

    for index, space in enumerate(selection.spaces):
        batch_id = f"{cycle}_{space}"
        planned_candidates = counts.get(space, 1)
        _runtime_event(
            resolved_root,
            EVENT_AUTORESEARCH_REPLAY_START,
            cycle_id=cycle,
            space=space,
            batch_id=batch_id,
            planned_candidates=planned_candidates,
        )
        try:
            batch = batch_func(
                space_name=space,
                n=planned_candidates,
                seed=effective_seed + index,
                mode=resolved_config.batch_mode,
                root=resolved_root,
                batch_id=batch_id,
                regenerate_baseline=resolved_config.regenerate_reports,
                regenerate_replay=True,
                regenerate_func=regenerate_func,
            )
            batches.append(batch)
            resolved_batch_id = str(_result_field(batch, "batch_id") or batch_id)
            health = _batch_health(batch, planned_candidates=planned_candidates)
            completed_candidates += int(health["completed"])
            if int(health["failed"]) > 0:
                failure = _batch_failure_message(space, resolved_batch_id, health)
                failures.append(failure)
                _runtime_event(
                    resolved_root,
                    EVENT_AUTORESEARCH_ERROR,
                    cycle_id=cycle,
                    space=space,
                    batch_id=resolved_batch_id,
                    status=str(health["status"]),
                    error=failure,
                )
            _runtime_event(
                resolved_root,
                EVENT_AUTORESEARCH_CANDIDATES_GENERATED,
                cycle_id=cycle,
                space=space,
                batch_id=resolved_batch_id,
                candidates_generated=int(health["generated"]),
            )
            _runtime_event(
                resolved_root,
                EVENT_AUTORESEARCH_REPLAY_DONE,
                cycle_id=cycle,
                space=space,
                batch_id=resolved_batch_id,
                status=str(health["status"]),
                completed=int(health["completed"]),
                skipped=int(health["skipped"]),
                failed=int(health["failed"]),
            )
            _runtime_event(
                resolved_root,
                EVENT_AUTORESEARCH_SCOREBOARD_UPDATED,
                cycle_id=cycle,
                space=space,
                batch_id=resolved_batch_id,
                scoreboard_path=str(_result_field(batch, "scoreboard_path") or research_runs_dir(resolved_root) / "scoreboard.json"),
                result_count=len(_batch_results(batch)),
            )
            _emit_candidate_result_events(resolved_root, cycle, space, batch)
        except Exception as exc:
            failures.append(f"batch_failed:{space}:{exc}")
            _runtime_event(
                resolved_root,
                EVENT_AUTORESEARCH_REPLAY_DONE,
                cycle_id=cycle,
                space=space,
                batch_id=batch_id,
                status="failed",
                error=str(exc),
            )
            _runtime_event(
                resolved_root,
                EVENT_AUTORESEARCH_ERROR,
                cycle_id=cycle,
                space=space,
                batch_id=batch_id,
                error=f"batch_failed:{space}:{exc}",
            )

    paper_candidate: dict[str, Any] | None = None
    paper_forward_start: dict[str, Any] | None = None
    all_results = [result for batch in batches for result in _batch_results(batch)]
    best = _best_accepted_replay(all_results, root=resolved_root)
    candidate_path = _candidate_policy_path(resolved_root, str(_result_field(best, "run_id") or "")) if best else None
    if best is None:
        accepted_without_event_replay = any(
            _result_field(result, "status") == STATUS_ACCEPTED_REPLAY for result in all_results
        )
        _runtime_event(
            resolved_root,
            EVENT_AUTORESEARCH_PAPER_PROMOTION_SKIPPED,
            cycle_id=cycle,
            reason="accepted_replay_missing_event_replay"
            if accepted_without_event_replay
            else "no_accepted_replay_candidate",
        )
    elif candidate_path is None:
        _runtime_event(
            resolved_root,
            EVENT_AUTORESEARCH_PAPER_PROMOTION_SKIPPED,
            cycle_id=cycle,
            reason="accepted_replay_candidate_policy_missing",
            run_id=str(_result_field(best, "run_id") or ""),
            proposal_id=str(_result_field(best, "proposal_id") or ""),
        )
    else:
        candidate_policy = _read_json(candidate_path)
        if not isinstance(candidate_policy, dict):
            raise AutoResearchSchedulerError(f"candidate_policy_unreadable:{candidate_path}")
        profile_id = str(
            candidate_policy.get("proposal_id")
            or _result_field(best, "proposal_id")
            or _result_field(best, "run_id")
            or "cycle_candidate"
        )
        try:
            promotion = promote_to_paper_candidate(
                candidate_path,
                evaluation_result=STATUS_ACCEPTED_REPLAY,
                root=resolved_root,
                profile_id=profile_id,
                promotion_report_path=research_runs_dir(resolved_root) / "logs" / f"{cycle}_paper_candidate_export.json",
            )
        except Exception as exc:
            _runtime_event(
                resolved_root,
                EVENT_AUTORESEARCH_ERROR,
                cycle_id=cycle,
                phase="paper_candidate_export",
                error=str(exc),
            )
            raise

        scoreboard_entry = _mark_scoreboard_needs_paper(resolved_root, best, candidate_policy, promotion)
        paper_candidate = _paper_candidate_payload(
            result=best,
            candidate_path=candidate_path,
            promotion=promotion,
            scoreboard_entry=scoreboard_entry,
            auto_paper_promote=resolved_config.auto_paper_promote,
        )
        _runtime_event(
            resolved_root,
            EVENT_AUTORESEARCH_PAPER_PROMOTION_CREATED,
            cycle_id=cycle,
            phase="paper_candidate_export",
            status=STATUS_NEEDS_PAPER,
            run_id=str(paper_candidate.get("run_id") or ""),
            proposal_id=str(paper_candidate.get("proposal_id") or ""),
            profile_path=str(promotion.profile_path),
            candidate_policy_path=str(candidate_path),
        )
        if not resolved_config.auto_paper_promote:
            _runtime_event(
                resolved_root,
                EVENT_AUTORESEARCH_PAPER_PROMOTION_SKIPPED,
                cycle_id=cycle,
                reason="auto_paper_promote_disabled",
                status=STATUS_NEEDS_PAPER,
                profile_path=str(promotion.profile_path),
            )
        else:
            starter = paper_start_func or start_paper_forward
            start_kwargs: dict[str, Any] = {
                "root": resolved_root,
                "run_id": f"paper_{_result_field(best, 'run_id')}",
                "profile_id": profile_id,
                "evaluation_result": STATUS_NEEDS_PAPER,
                "allow_needs_paper": True,
            }
            if paper_start_func is None:
                start_kwargs["promotion"] = promotion
            try:
                paper = starter(candidate_path, **start_kwargs)
            except Exception as exc:
                _runtime_event(
                    resolved_root,
                    EVENT_AUTORESEARCH_ERROR,
                    cycle_id=cycle,
                    phase="paper_forward_start",
                    error=str(exc),
                )
                raise
            paper_forward_start = _as_dict(paper)
            _runtime_event(
                resolved_root,
                EVENT_AUTORESEARCH_PAPER_PROMOTION_CREATED,
                cycle_id=cycle,
                phase="paper_forward_start",
                run_id=str(paper_forward_start.get("run_id") or ""),
                state_path=str(paper_forward_start.get("state_path") or ""),
                candidate_policy_path=str(paper_forward_start.get("candidate_policy_path") or ""),
            )

    status = "completed"
    if failures:
        status = "degraded" if completed_candidates > 0 else "failed"
    result = AutoResearchCycleResult(
        cycle_id=cycle,
        status=status,
        config=resolved_config,
        selected_spaces=selection.spaces,
        idle_trigger=selection.idle_trigger,
        report_bundle_path=report_bundle_path,
        batches=batches,
        paper_candidate=paper_candidate,
        paper_forward_start=paper_forward_start,
        demotion=demotion,
        warnings=warnings,
        failures=failures,
        cycle_report_path=_cycle_report_path(resolved_root, cycle),
    )
    _write_cycle_report(resolved_root, result)
    _runtime_event(
        resolved_root,
        EVENT_AUTORESEARCH_CYCLE_END,
        cycle_id=cycle,
        status=status,
        failures=list(failures),
        warnings=list(warnings),
    )
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

    if paper_metrics is None and state.get("activation_status") != "applied":
        result = PaperDemotionResult(
            checked=False, run_id=str(state.get("run_id") or run_dir.name),
            status="inactive_candidate_profile",
            warnings=["exported_profile_is_not_an_applied_runtime_policy"],
            demotion_report_path=run_dir / "demotion_report.json",
        )
        _write_demotion_reports(resolved_root, run_dir, result)
        return result

    resolved_baseline = dict(baseline_metrics) if baseline_metrics is not None else {}
    if not resolved_baseline:
        payload = _read_json(run_dir / "baseline_metrics.json")
        if isinstance(payload, dict):
            resolved_baseline = payload
    resolved_paper = dict(paper_metrics) if paper_metrics is not None else _load_current_paper_metrics(resolved_root, state)

    if (not resolved_baseline or not resolved_paper
            or (paper_metrics is None and (resolved_paper.get("evidence_rejections")
                                         or not resolved_paper.get("closed_trades")
                                         or resolved_paper.get("uncosted_records")))):
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
    "SKIPPED_NO_DATA",
    "SKIPPED_RECENT_CYCLE",
    "SpaceSelection",
    "detect_idle_trigger",
    "evaluate_paper_profitability_for_demotion",
    "load_scheduler_config",
    "moonshot_pressure_reasons",
    "run_autoresearch_cycle",
    "run_autoresearch_loop",
    "select_research_spaces",
    "validate_scheduler_config",
]
