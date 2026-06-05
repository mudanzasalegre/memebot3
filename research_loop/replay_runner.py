from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from research_loop.api_budget import build_api_budget_report, metrics_from_api_budget
from research_loop.candidate_diff import write_candidate_diff
from research_loop.paths import metrics_dir, project_root
from research_loop.sandbox import SandboxResult, create_candidate_sandbox

REPLAY_REPORTS = (
    "policy_replay.json",
    "trade_diagnostics.json",
    "missed_pumps.json",
    "runner_capture_ladder_report.json",
    "entry_funnel_blockers_report.json",
    "bot_profitability_health.json",
    "current_run_summary.json",
    "current_run_trade_diagnostics.json",
    "current_run_funnel.json",
    "current_run_missed_pumps.json",
    "current_run_lane_summary.json",
    "lane_sizing_report.json",
    "pump_entry_lane_selector_report.json",
    "shadow_followup_micro_report.json",
    "moonshot_micro_lottery_report.json",
)


@dataclass(frozen=True)
class ReplayRunResult:
    run_id: str
    run_dir: Path
    status: str
    replay_metrics_path: Path
    report_snapshot_dir: Path
    replay_metrics: dict[str, Any]
    warnings: list[str]
    failures: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "status": self.status,
            "replay_metrics_path": str(self.replay_metrics_path),
            "report_snapshot_dir": str(self.report_snapshot_dir),
            "replay_metrics": self.replay_metrics,
            "warnings": list(self.warnings),
            "failures": list(self.failures),
        }


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _copy_snapshot(root: Path, snapshot_dir: Path) -> list[str]:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    for name in REPLAY_REPORTS:
        source = metrics_dir(root) / name
        if not source.exists():
            failures.append(f"missing_report:{name}")
            continue
        shutil.copy2(source, snapshot_dir / name)
    return failures


def _candidate_env_path(sandbox: SandboxResult | str | Path, run_dir: Path) -> Path | None:
    if isinstance(sandbox, SandboxResult):
        return sandbox.candidate_env_path if sandbox.candidate_env_path.exists() else None
    path = run_dir / "candidate.env"
    return path if path.exists() else None


def _regenerate_core_reports_with_profile(root: Path, profile_path: Path) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    summary_path = profile_path.parent / "candidate_regeneration_summary.json"
    env = os.environ.copy()
    env["CONFIG_PROFILE_PATH"] = str(profile_path)
    env.pop("CONFIG_PROFILE", None)
    env["AUTORESEARCH_ROOT"] = str(root)
    env["AUTORESEARCH_REGEN_SUMMARY_PATH"] = str(summary_path)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(repo_root) if not existing_pythonpath else f"{repo_root}{os.pathsep}{existing_pythonpath}"
    code = (
        "import json, os\n"
        "from pathlib import Path\n"
        "from analytics.core_report_scheduler import regenerate_core_reports\n"
        "root = Path(os.environ['AUTORESEARCH_ROOT'])\n"
        "summary_path = Path(os.environ['AUTORESEARCH_REGEN_SUMMARY_PATH'])\n"
        "summary = regenerate_core_reports(root, include_test_events=False)\n"
        "summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding='utf-8')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip().replace("\n", " | ")
        raise RuntimeError(stderr or f"subprocess_exit_{completed.returncode}")
    summary = _read_json(summary_path)
    return summary if isinstance(summary, dict) else {}


def _group_trades(report: dict[str, Any], group_name: str) -> int:
    groups = report.get("groups") if isinstance(report, dict) else {}
    group = groups.get(group_name) if isinstance(groups, dict) else {}
    if not isinstance(group, dict):
        return 0
    try:
        return int(float(group.get("trades") or 0))
    except (TypeError, ValueError):
        return 0


def _float(payload: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(payload.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _int(payload: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(float(payload.get(key, default) or default))
    except (TypeError, ValueError):
        return default


def extract_replay_metrics(snapshot_dir: str | Path, *, api_budget: dict[str, Any] | None = None) -> dict[str, Any]:
    base = Path(snapshot_dir)
    policy_replay = _read_json(base / "policy_replay.json")
    trade_diagnostics = _read_json(base / "trade_diagnostics.json")
    health = _read_json(base / "bot_profitability_health.json")
    runner = _read_json(base / "runner_capture_ladder_report.json")
    moonshot = _read_json(base / "moonshot_micro_lottery_report.json")
    missed = _read_json(base / "missed_pumps.json")
    current_summary = _read_json(base / "current_run_summary.json")

    current_policy = policy_replay.get("current") if isinstance(policy_replay, dict) else {}
    if not isinstance(current_policy, dict):
        current_policy = {}
    trade_summary = trade_diagnostics.get("summary") if isinstance(trade_diagnostics, dict) else {}
    if not isinstance(trade_summary, dict):
        trade_summary = {}
    runner_summary = runner.get("summary") if isinstance(runner, dict) else {}
    if not isinstance(runner_summary, dict):
        runner_summary = {}
    if not isinstance(current_summary, dict):
        current_summary = {}

    metrics: dict[str, Any] = {
        "total_pnl_usd": _float(current_policy, "total_pnl", _float(trade_summary, "total_pnl_points")),
        "avg_pnl_pct": _float(current_policy, "avg_pnl", _float(trade_summary, "avg_pnl")),
        "median_pnl_pct": _float(current_policy, "median_pnl", _float(trade_summary, "median_pnl")),
        "win_rate_pct": _float(current_policy, "win_rate", _float(trade_summary, "win_rate")),
        "closed_trades": _int(current_policy, "trades", _int(trade_summary, "trades")),
        "buys_per_hour": _float(health, "buys_per_hour"),
        "runner_capture_ratio": _float(current_policy, "runner_capture_ratio"),
        "runner_capture_ladder_ratio": _float(runner_summary, "avg_current_capture_ratio"),
        "realized_pnl_on_runners": _float(runner_summary, "avg_simulated_realized_pnl_pct"),
        "moonshot_peak100_capture": _float(moonshot, "peak100_captured"),
        "moonshot_peak500_capture": _float(moonshot, "peak500_captured"),
        "moonshot_peak1000_capture": _float(moonshot, "peak1000_captured"),
        "moonshot_micro_tail_capture_ratio": _float(moonshot, "tail_capture_ratio"),
        "severe_loss_count": _int(current_policy, "severe_loss_count", _int(trade_summary, "severe_loss_count")),
        "liquidity_crush_count": _int(current_policy, "liq_crush_count", _int(trade_summary, "liq_crush_count")),
        "adverse_tick_count": _int(current_policy, "adverse_tick_count", _int(trade_summary, "adverse_tick_count")),
        "stop_loss_count": _group_trades(trade_diagnostics, "exit_reason:STOP_LOSS"),
        "no_pump_exit_count": _group_trades(trade_diagnostics, "exit_reason:NO_PUMP_EXIT"),
        "max_drawdown_proxy": _float(current_policy, "max_drawdown_proxy"),
        "giveback_pct": _float(runner_summary, "avg_current_giveback_pct"),
        "overtrading_count": _int(health, "overtrading_count", _int(current_summary, "overtrading_count")),
        "idle_no_buy_hours": _float(
            current_summary,
            "idle_no_buy_hours",
            _float(current_summary, "hours_since_last_buy", _float(health, "idle_no_buy_hours")),
        ),
        "missed_peak100_count": 0,
        "missed_peak500_count": 0,
        "missed_peak1000_count": 0,
    }

    if isinstance(health, dict):
        missed_peaks = health.get("missed_peak_100_500_1000")
        if isinstance(missed_peaks, dict):
            metrics["missed_peak100_count"] = _int(missed_peaks, "peak_100")
            metrics["missed_peak500_count"] = _int(missed_peaks, "peak_500")
            metrics["missed_peak1000_count"] = _int(missed_peaks, "peak_1000")
    elif isinstance(missed, list):
        metrics["missed_peak100_count"] = len([row for row in missed if isinstance(row, dict) and _float(row, "peak_pct") >= 100])
        metrics["missed_peak500_count"] = len([row for row in missed if isinstance(row, dict) and _float(row, "peak_pct") >= 500])
        metrics["missed_peak1000_count"] = len([row for row in missed if isinstance(row, dict) and _float(row, "peak_pct") >= 1000])

    if api_budget is not None:
        metrics.update(metrics_from_api_budget(api_budget))
    return metrics


def run_research_replay(
    candidate_policy: str | Path | dict[str, Any],
    *,
    root: str | Path | None = None,
    run_id: str | None = None,
    regenerate: bool = True,
    regenerate_func: Callable[[Path], dict[str, Any]] | None = None,
) -> ReplayRunResult:
    resolved_root = project_root(root)
    sandbox = create_candidate_sandbox(candidate_policy, root=resolved_root, run_id=run_id)
    return run_research_replay_from_sandbox(
        sandbox,
        root=resolved_root,
        regenerate=regenerate,
        regenerate_func=regenerate_func,
    )


def run_research_replay_from_sandbox(
    sandbox: SandboxResult | str | Path,
    *,
    root: str | Path | None = None,
    regenerate: bool = True,
    regenerate_func: Callable[[Path], dict[str, Any]] | None = None,
) -> ReplayRunResult:
    resolved_root = project_root(root)
    if isinstance(sandbox, SandboxResult):
        run_id = sandbox.run_id
        run_dir = sandbox.run_dir
        candidate_policy_path = sandbox.candidate_policy_path
    else:
        run_dir = Path(sandbox)
        run_id = run_dir.name
        candidate_policy_path = run_dir / "candidate_policy.json"
    candidate_env_path = _candidate_env_path(sandbox, run_dir)

    warnings: list[str] = []
    failures: list[str] = []
    if regenerate:
        try:
            if regenerate_func is None and candidate_env_path is not None:
                summary = _regenerate_core_reports_with_profile(resolved_root, candidate_env_path)
            elif regenerate_func is None:
                from analytics.core_report_scheduler import regenerate_core_reports

                summary = regenerate_core_reports(resolved_root, include_test_events=False)
            else:
                if candidate_env_path is not None:
                    warnings.append("candidate_profile_not_applied_custom_regenerate_func")
                summary = regenerate_func(resolved_root)
            regen_warnings = summary.get("warnings") if isinstance(summary, dict) else {}
            if regen_warnings:
                warnings.extend(f"report_warning:{name}:{message}" for name, message in regen_warnings.items())
        except Exception as exc:
            failures.append(f"regenerate_core_reports_failed:{exc}")

    report_snapshot_dir = run_dir / "report_snapshot"
    failures.extend(_copy_snapshot(resolved_root, report_snapshot_dir))
    api_budget = build_api_budget_report(resolved_root, write=True)
    replay_metrics = extract_replay_metrics(report_snapshot_dir, api_budget=api_budget)
    if failures:
        replay_metrics["failed"] = True
    replay_metrics_path = run_dir / "replay_metrics.json"
    _write_json(replay_metrics_path, replay_metrics)
    if candidate_policy_path.exists():
        write_candidate_diff(candidate_policy_path, run_dir=run_dir)

    status = "failed" if failures else "completed"
    _write_json(
        run_dir / "replay_result.json",
        {
            "run_id": run_id,
            "status": status,
            "warnings": warnings,
            "failures": failures,
            "replay_metrics_path": str(replay_metrics_path),
            "report_snapshot_dir": str(report_snapshot_dir),
            "candidate_env_path": str(candidate_env_path) if candidate_env_path is not None else None,
        },
    )
    return ReplayRunResult(
        run_id=run_id,
        run_dir=run_dir,
        status=status,
        replay_metrics_path=replay_metrics_path,
        report_snapshot_dir=report_snapshot_dir,
        replay_metrics=replay_metrics,
        warnings=warnings,
        failures=failures,
    )


__all__ = [
    "REPLAY_REPORTS",
    "ReplayRunResult",
    "extract_replay_metrics",
    "run_research_replay",
    "run_research_replay_from_sandbox",
]
