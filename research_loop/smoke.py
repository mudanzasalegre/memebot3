from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_loop.api_budget import build_api_budget_report
from research_loop.candidate_generator import generate_candidate_policies, write_candidate_policies
from research_loop.evaluator import EvaluationResult, STATUS_ACCEPTED_REPLAY, evaluate_replay_candidate
from research_loop.experiment_schema import validate_candidate_policy
from research_loop.paths import metrics_dir, project_root, research_runs_dir
from research_loop.policy_promoter import PromotionResult, promote_to_paper_candidate
from research_loop.replay_runner import REPLAY_REPORTS, ReplayRunResult, run_research_replay_from_sandbox
from research_loop.safety import SafetyResult, validate_candidate_safety
from research_loop.sandbox import SECRET_MARKERS, SandboxResult, create_candidate_sandbox
from research_loop.scoreboard import load_scoreboard, record_evaluation

FALSE_LIVE_KEYS = (
    "LIVE_CANARY_ENABLED",
    "GREEN_SNIPER_LIVE_ENABLED",
    "RESEARCH_RANK_CANARY_LIVE_ENABLED",
    "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED",
    "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED",
    "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED",
    "LATE_MOMENTUM_WATCH_LIVE_ENABLED",
    "AUTO_PROMOTE_LIVE",
    "MODEL_AUTO_PROMOTE",
    "ML_AUTO_PROMOTE_LANES",
    "AUTORESEARCH_LIVE_PROMOTION_ENABLED",
    "AUTORESEARCH_AUTO_LIVE_PROMOTE",
    "LLM_TRADING_ENABLED",
    "AUTORESEARCH_LLM_CAN_TOUCH_LIVE",
)


@dataclass(frozen=True)
class SmokeCandidateResult:
    proposal_id: str
    run_id: str
    safety: dict[str, Any]
    sandbox: dict[str, Any]
    replay: dict[str, Any]
    evaluation: dict[str, Any]
    scoreboard_entry: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "run_id": self.run_id,
            "safety": self.safety,
            "sandbox": self.sandbox,
            "replay": self.replay,
            "evaluation": self.evaluation,
            "scoreboard_entry": self.scoreboard_entry,
        }


@dataclass(frozen=True)
class AutoResearchSmokeResult:
    status: str
    smoke_id: str
    root: Path
    candidates_generated: int
    results: list[SmokeCandidateResult] = field(default_factory=list)
    api_budget_path: Path | None = None
    scoreboard_path: Path | None = None
    paper_profile_path: Path | None = None
    report_path: Path | None = None
    live_remains_false: bool = False
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "smoke_id": self.smoke_id,
            "root": str(self.root),
            "candidates_generated": self.candidates_generated,
            "results": [result.as_dict() for result in self.results],
            "api_budget_path": str(self.api_budget_path) if self.api_budget_path else None,
            "scoreboard_path": str(self.scoreboard_path) if self.scoreboard_path else None,
            "paper_profile_path": str(self.paper_profile_path) if self.paper_profile_path else None,
            "report_path": str(self.report_path) if self.report_path else None,
            "live_remains_false": self.live_remains_false,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-").lower()
    return cleaned or "autoresearch_smoke"


def _default_smoke_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("ar_smoke_%Y%m%d_%H%M%S")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip().upper()] = value.strip().strip('"').strip("'")
    return values


def _truthy_text(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _falsey_text(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "n", "off"}


def _fixture_payloads() -> dict[str, Any]:
    policy_current = {
        "total_pnl": 14.0,
        "avg_pnl": 3.0,
        "median_pnl": 2.0,
        "win_rate": 52.0,
        "trades": 12,
        "runner_capture_ratio": 0.28,
        "severe_loss_count": 0,
        "liq_crush_count": 0,
        "adverse_tick_count": 0,
        "max_drawdown_proxy": 0.0,
    }
    trade_summary = {
        "total_pnl_points": 14.0,
        "avg_pnl": 3.0,
        "median_pnl": 2.0,
        "win_rate": 52.0,
        "trades": 12,
        "severe_loss_count": 0,
        "liq_crush_count": 0,
        "adverse_tick_count": 0,
    }
    return {
        "policy_replay.json": {"current": policy_current},
        "trade_diagnostics.json": {
            "summary": trade_summary,
            "groups": {
                "exit_reason:STOP_LOSS": {"trades": 0},
                "exit_reason:NO_PUMP_EXIT": {"trades": 0},
            },
        },
        "missed_pumps.json": [],
        "runner_capture_ladder_report.json": {
            "summary": {
                "avg_current_capture_ratio": 0.3,
                "avg_simulated_realized_pnl_pct": 4.0,
                "avg_current_giveback_pct": 0.0,
            }
        },
        "entry_funnel_blockers_report.json": {"summary": {}, "rows": []},
        "bot_profitability_health.json": {
            "buys_per_hour": 0.8,
            "missed_peak_100_500_1000": {"peak_100": 0, "peak_500": 0, "peak_1000": 0},
        },
        "current_run_summary.json": {
            "elapsed_hours": 6.0,
            "closed_trades": 12,
            "decisions": 120,
            "daily_buys": 3,
            "total_pnl_usd": 14.0,
            "avg_pnl_pct": 3.0,
            "median_pnl_pct": 2.0,
            "win_rate_pct": 52.0,
            "runner_capture_ratio": 0.28,
        },
        "current_run_trade_diagnostics.json": trade_summary,
        "current_run_funnel.json": {"summary": {}},
        "current_run_missed_pumps.json": [],
        "current_run_lane_summary.json": {"lanes": []},
        "lane_sizing_report.json": {"summary": {}},
        "pump_entry_lane_selector_report.json": {"summary": {}},
        "shadow_followup_micro_report.json": {"summary": {}},
        "moonshot_micro_lottery_report.json": {
            "peak100_captured": 1.0,
            "peak500_captured": 0.0,
            "peak1000_captured": 0.0,
            "tail_capture_ratio": 0.2,
        },
    }


def ensure_smoke_metrics(root: str | Path | None = None, *, overwrite: bool = False) -> list[str]:
    resolved_root = project_root(root)
    created: list[str] = []
    payloads = _fixture_payloads()
    for name in REPLAY_REPORTS:
        path = metrics_dir(resolved_root) / name
        if path.exists() and not overwrite:
            continue
        _write_json(path, payloads.get(name, {}))
        created.append(name)
    return created


def ensure_safe_source_profile(root: str | Path | None = None) -> Path:
    resolved_root = project_root(root)
    profile = resolved_root / "config" / "profiles" / "paper_hotfix_runner_v2.env"
    if profile.exists():
        return profile
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        "\n".join(
            [
                "DRY_RUN=1",
                "PAPER_SNIPER_MODE=true",
                "STRATEGY_OPTIMIZATION_LOCK=true",
                "LIVE_CANARY_ENABLED=false",
                "GREEN_SNIPER_LIVE_ENABLED=false",
                "AUTO_PROMOTE_LIVE=false",
                "MODEL_AUTO_PROMOTE=false",
                "AUTORESEARCH_LIVE_PROMOTION_ENABLED=false",
                "AUTORESEARCH_AUTO_LIVE_PROMOTE=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return profile


def _baseline_from_replay_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    baseline = dict(metrics)

    def value(key: str, default: float) -> float:
        try:
            return float(metrics.get(key, default))
        except (TypeError, ValueError):
            return default

    baseline.update(
        {
            "total_pnl_usd": value("total_pnl_usd", 1.0) - 1.0,
            "avg_pnl_pct": value("avg_pnl_pct", 1.0) - 0.5,
            "median_pnl_pct": value("median_pnl_pct", 1.0) - 0.5,
            "win_rate_pct": value("win_rate_pct", 1.0) - 1.0,
            "runner_capture_ratio": value("runner_capture_ratio", 0.1) - 0.02,
            "moonshot_peak100_capture": max(0.0, value("moonshot_peak100_capture", 0.0) - 0.1),
            "moonshot_peak500_capture": max(0.0, value("moonshot_peak500_capture", 0.0) - 0.1),
            "moonshot_peak1000_capture": max(0.0, value("moonshot_peak1000_capture", 0.0) - 0.1),
        }
    )
    for key in (
        "severe_loss_count",
        "liquidity_crush_count",
        "adverse_tick_count",
        "api_429_count",
        "provider_degraded_minutes",
        "overtrading_count",
        "idle_no_buy_hours",
        "max_drawdown_proxy",
    ):
        baseline[key] = metrics.get(key, 0)
    return baseline


def _with_smoke_ids(candidates: list[dict[str, Any]], smoke_id: str) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        payload = dict(candidate)
        suffix = str(candidate.get("proposal_id") or f"{index:02d}")[-10:]
        payload["proposal_id"] = _safe_id(f"{smoke_id}_{index:02d}_{suffix}")
        payload["created_at_utc"] = utc_now()
        validate_candidate_policy(payload)
        updated.append(payload)
    return updated


def _policy_live_is_false(candidate: dict[str, Any]) -> bool:
    if candidate.get("live_allowed") is not False:
        return False
    changes = candidate.get("changes") if isinstance(candidate.get("changes"), dict) else {}
    for key in FALSE_LIVE_KEYS:
        if key in changes and _truthy_text(str(changes.get(key))):
            return False
    return True


def _env_has_no_secrets(values: dict[str, str]) -> bool:
    for key in values:
        upper = key.upper()
        if any(marker in upper for marker in SECRET_MARKERS):
            return False
        if "WALLET" in upper:
            return False
    return True


def _candidate_env_is_safe(path: Path) -> bool:
    values = _parse_env(path)
    if not _truthy_text(values.get("DRY_RUN")):
        return False
    for key in FALSE_LIVE_KEYS:
        if key in values and not _falsey_text(values.get(key)):
            return False
    return _env_has_no_secrets(values)


def _paper_profile_is_safe(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    values = _parse_env(path)
    if not _truthy_text(values.get("DRY_RUN")):
        return False
    for key in FALSE_LIVE_KEYS:
        if key in values and not _falsey_text(values.get(key)):
            return False
    return _env_has_no_secrets(values)


def _best_accepted(results: list[tuple[dict[str, Any], EvaluationResult]]) -> tuple[dict[str, Any], EvaluationResult] | None:
    accepted = [item for item in results if item[1].status == STATUS_ACCEPTED_REPLAY]
    if not accepted:
        return None
    return max(accepted, key=lambda item: item[1].objective.score if item[1].objective else 0.0)


def _write_smoke_report(root: Path, smoke_id: str, payload: dict[str, Any]) -> Path:
    report_path = research_runs_dir(root) / "logs" / f"{smoke_id}.json"
    _write_json(report_path, payload)
    _write_json(research_runs_dir(root) / "logs" / "autoresearch_smoke_latest.json", payload)
    return report_path


def run_autoresearch_smoke(
    *,
    root: str | Path | None = None,
    space_name: str = "moonshot_micro",
    n: int = 3,
    seed: int | None = 33,
    mode: str = "seeded_random",
    smoke_id: str | None = None,
    overwrite_fixture_metrics: bool = False,
    regenerate_replay_reports: bool = False,
) -> AutoResearchSmokeResult:
    resolved_root = project_root(root)
    resolved_smoke_id = _safe_id(smoke_id or _default_smoke_id())
    failures: list[str] = []
    warnings: list[str] = []
    created_metrics = ensure_smoke_metrics(resolved_root, overwrite=overwrite_fixture_metrics)
    if created_metrics:
        warnings.append("created_missing_fixture_metrics:" + ",".join(created_metrics))
    ensure_safe_source_profile(resolved_root)

    api_budget = build_api_budget_report(resolved_root, write=True)
    api_budget_path = research_runs_dir(resolved_root) / "api_budget.json"
    candidates = _with_smoke_ids(
        generate_candidate_policies(space_name=space_name, n=n, mode=mode, seed=seed),
        resolved_smoke_id,
    )
    write_candidate_policies(candidates, root=resolved_root)

    smoke_results: list[SmokeCandidateResult] = []
    accepted_pairs: list[tuple[dict[str, Any], EvaluationResult]] = []
    generated_artifacts_safe = True

    for index, candidate in enumerate(candidates):
        proposal_id = str(candidate.get("proposal_id") or f"candidate_{index}")
        safety: SafetyResult = validate_candidate_safety(candidate)
        if not safety.ok:
            failures.append(f"safety_failed:{proposal_id}:{','.join(safety.errors)}")
        sandbox: SandboxResult = create_candidate_sandbox(
            candidate,
            root=resolved_root,
            run_id=f"{resolved_smoke_id}_{index:02d}_{proposal_id}",
        )
        replay: ReplayRunResult = run_research_replay_from_sandbox(
            sandbox,
            root=resolved_root,
            regenerate=regenerate_replay_reports,
        )
        baseline_metrics = _baseline_from_replay_metrics(replay.replay_metrics)
        _write_json(sandbox.run_dir / "baseline_metrics.json", baseline_metrics)
        evaluation = evaluate_replay_candidate(candidate, baseline_metrics, replay.replay_metrics)
        accepted_pairs.append((candidate, evaluation))
        scoreboard_entry = record_evaluation(
            run_id=sandbox.run_id,
            candidate_policy=candidate,
            evaluation_result=evaluation,
            root=resolved_root,
        )
        generated_artifacts_safe = (
            generated_artifacts_safe
            and _policy_live_is_false(candidate)
            and _candidate_env_is_safe(sandbox.candidate_env_path)
        )
        if replay.status != "completed":
            failures.append(f"replay_not_completed:{proposal_id}:{','.join(replay.failures)}")
        if evaluation.status not in {STATUS_ACCEPTED_REPLAY, "needs_paper", "rejected", "inconclusive"}:
            failures.append(f"unexpected_evaluation_status:{proposal_id}:{evaluation.status}")

        smoke_results.append(
            SmokeCandidateResult(
                proposal_id=proposal_id,
                run_id=sandbox.run_id,
                safety=safety.as_dict(),
                sandbox=sandbox.as_dict(),
                replay=replay.as_dict(),
                evaluation=evaluation.as_dict(),
                scoreboard_entry=scoreboard_entry,
            )
        )

    best = _best_accepted(accepted_pairs)
    promotion: PromotionResult | None = None
    if best is None:
        failures.append("no_accepted_replay_candidate_for_paper_profile")
    else:
        best_candidate, best_evaluation = best
        promotion = promote_to_paper_candidate(
            best_candidate,
            evaluation_result=best_evaluation,
            root=resolved_root,
            profile_id=resolved_smoke_id,
            promotion_report_path=research_runs_dir(resolved_root)
            / "logs"
            / f"{resolved_smoke_id}_paper_profile_export.json",
        )

    scoreboard_path = research_runs_dir(resolved_root) / "scoreboard.json"
    if not scoreboard_path.exists() or not load_scoreboard(resolved_root):
        failures.append("scoreboard_not_updated")
    if not api_budget_path.exists() or not isinstance(api_budget, dict):
        failures.append("api_budget_not_checked")

    paper_profile_path = promotion.profile_path if promotion is not None else None
    live_remains_false = generated_artifacts_safe and _paper_profile_is_safe(paper_profile_path)
    if not live_remains_false:
        failures.append("live_safety_invariant_failed")

    report_path = research_runs_dir(resolved_root) / "logs" / f"{resolved_smoke_id}.json"
    result = AutoResearchSmokeResult(
        status="ok" if not failures else "failed",
        smoke_id=resolved_smoke_id,
        root=resolved_root,
        candidates_generated=len(candidates),
        results=smoke_results,
        api_budget_path=api_budget_path,
        scoreboard_path=scoreboard_path,
        paper_profile_path=paper_profile_path,
        report_path=report_path,
        live_remains_false=live_remains_false,
        failures=failures,
        warnings=warnings,
    )
    _write_smoke_report(resolved_root, resolved_smoke_id, result.as_dict())
    return result


__all__ = [
    "AutoResearchSmokeResult",
    "SmokeCandidateResult",
    "ensure_safe_source_profile",
    "ensure_smoke_metrics",
    "run_autoresearch_smoke",
]
