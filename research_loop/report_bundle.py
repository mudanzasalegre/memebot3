from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from research_loop.api_budget import build_api_budget_report
from research_loop.paths import metrics_dir, project_root, research_runs_dir

REPORT_FILES = {
    "current_run": {
        "summary": "current_run_summary.json",
        "trade_diagnostics": "current_run_trade_diagnostics.json",
        "funnel": "current_run_funnel.json",
        "missed_pumps": "current_run_missed_pumps.json",
        "lane_summary": "current_run_lane_summary.json",
    },
    "historical": {
        "bot_profitability_health": "bot_profitability_health.json",
        "missed_pumps": "missed_pumps.json",
        "policy_replay": "policy_replay.json",
    },
    "lanes": {
        "lane_sizing": "lane_sizing_report.json",
        "pump_entry_lane_selector": "pump_entry_lane_selector_report.json",
        "shadow_followup_micro": "shadow_followup_micro_report.json",
    },
    "exits": {
        "runner_capture_ladder": "runner_capture_ladder_report.json",
    },
    "moonshots": {
        "moonshot_micro_lottery": "moonshot_micro_lottery_report.json",
        "runner_capture_ladder": "runner_capture_ladder_report.json",
        "missed_pumps": "missed_pumps.json",
    },
    "funnel": {
        "current_run_funnel": "current_run_funnel.json",
        "entry_funnel_blockers": "entry_funnel_blockers_report.json",
        "entry_funnel_blocker_samples": "entry_funnel_blocker_samples.json",
    },
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {
            "placeholder": True,
            "warning": "missing_report",
            "path": str(path),
            "rows": 0,
        }
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception as exc:
        return {
            "placeholder": True,
            "warning": f"unreadable_report:{exc}",
            "path": str(path),
            "rows": 0,
        }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _section(root: Path, section_name: str) -> dict[str, Any]:
    base = metrics_dir(root)
    return {name: _read_json(base / filename) for name, filename in REPORT_FILES[section_name].items()}


def _recommendation_context(bundle: dict[str, Any]) -> dict[str, Any]:
    health = bundle.get("historical", {}).get("bot_profitability_health", {})
    current = bundle.get("current_run", {}).get("summary", {})
    moonshot = bundle.get("moonshots", {}).get("moonshot_micro_lottery", {})
    return {
        "recommended_next_action": health.get("recommended_next_action") if isinstance(health, dict) else None,
        "current_run_closed_positions": current.get("closed_positions") if isinstance(current, dict) else None,
        "current_run_strategy_decisions": current.get("strategy_decisions") if isinstance(current, dict) else None,
        "moonshot_candidates_seen": moonshot.get("candidates_seen") if isinstance(moonshot, dict) else None,
        "source": "local_reports_only",
    }


def build_report_bundle(
    root: str | Path | None = None,
    *,
    write: bool = True,
    include_api_budget: bool = True,
) -> dict[str, Any]:
    resolved_root = project_root(root)
    bundle: dict[str, Any] = {
        "generated_at_utc": utc_now(),
        "current_run": _section(resolved_root, "current_run"),
        "historical": _section(resolved_root, "historical"),
        "lanes": _section(resolved_root, "lanes"),
        "exits": _section(resolved_root, "exits"),
        "moonshots": _section(resolved_root, "moonshots"),
        "api_budget": {},
        "funnel": _section(resolved_root, "funnel"),
        "recommendation_context": {},
    }
    if include_api_budget:
        bundle["api_budget"] = build_api_budget_report(resolved_root, write=write)
    else:
        bundle["api_budget"] = {
            "placeholder": True,
            "warning": "api_budget_not_requested",
        }
    bundle["recommendation_context"] = _recommendation_context(bundle)

    if write:
        _write_json(research_runs_dir(resolved_root) / "report_bundle_latest.json", bundle)
    return bundle


__all__ = ["REPORT_FILES", "build_report_bundle", "utc_now"]
