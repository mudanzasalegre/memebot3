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
        "autotune": "current_run_autotune_state.json",
    },
    "acquisition": {
        "health": "acquisition_health_report.json",
        "paper_bootstrap": "paper_bootstrap_report.json",
        "paper_real_outcomes": "paper_real_outcomes.json",
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

MAX_EMBEDDED_REPORT_ROWS = 100


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
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception as exc:
        return {
            "placeholder": True,
            "warning": f"unreadable_report:{exc}",
            "path": str(path),
            "rows": 0,
        }
    return _compact_report_payload(path, payload)


def _peak_value(row: dict[str, Any]) -> float:
    for key in ("confirmed_later_peak_pct", "later_max_pnl_pct", "peak_pct", "max_pnl_pct_seen", "max_pnl_pct"):
        try:
            value = row.get(key)
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _missed_summary(rows: list[Any]) -> dict[str, int]:
    dict_rows = [row for row in rows if isinstance(row, dict)]
    return {
        "missed": len(dict_rows),
        "peak_100": sum(1 for row in dict_rows if _peak_value(row) >= 100.0),
        "peak_500": sum(1 for row in dict_rows if _peak_value(row) >= 500.0),
        "peak_1000": sum(1 for row in dict_rows if _peak_value(row) >= 1000.0),
    }


def _compact_report_payload(path: Path, payload: Any) -> Any:
    if isinstance(payload, list):
        if len(payload) <= MAX_EMBEDDED_REPORT_ROWS:
            return payload
        compact: dict[str, Any] = {
            "rows": len(payload),
            "sample": payload[:MAX_EMBEDDED_REPORT_ROWS],
            "truncated": True,
            "truncated_from": "list",
        }
        if path.name == "missed_pumps.json":
            summary = _missed_summary(payload)
            compact["summary"] = summary
            compact["missed_peak100_count"] = summary["peak_100"]
            compact["missed_peak500_count"] = summary["peak_500"]
            compact["missed_peak1000_count"] = summary["peak_1000"]
        return compact
    if not isinstance(payload, dict):
        return payload

    compact = dict(payload)
    truncated = False
    rows_for_summary: list[Any] = []
    for key in ("data", "rows", "items", "samples"):
        rows = compact.get(key)
        if not isinstance(rows, list):
            continue
        if not rows_for_summary:
            rows_for_summary = rows
        if len(rows) > MAX_EMBEDDED_REPORT_ROWS:
            compact[f"{key}_total_rows"] = len(rows)
            compact[key] = rows[:MAX_EMBEDDED_REPORT_ROWS]
            truncated = True
    if path.name == "missed_pumps.json" and rows_for_summary:
        summary = _missed_summary(rows_for_summary)
        compact["summary"] = summary
        compact["missed_peak100_count"] = summary["peak_100"]
        compact["missed_peak500_count"] = summary["peak_500"]
        compact["missed_peak1000_count"] = summary["peak_1000"]
    if truncated:
        compact["truncated"] = True
        compact["embedded_row_limit"] = MAX_EMBEDDED_REPORT_ROWS
    return compact


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _section(root: Path, section_name: str, *, cache: dict[Path, Any] | None = None) -> dict[str, Any]:
    base = metrics_dir(root)
    section: dict[str, Any] = {}
    for name, filename in REPORT_FILES[section_name].items():
        path = base / filename
        if cache is not None and path in cache:
            section[name] = cache[path]
            continue
        payload = _read_json(path)
        if cache is not None:
            cache[path] = payload
        section[name] = payload
    return section


def _recommendation_context(bundle: dict[str, Any]) -> dict[str, Any]:
    health = bundle.get("historical", {}).get("bot_profitability_health", {})
    current = bundle.get("current_run", {}).get("summary", {})
    autotune = bundle.get("current_run", {}).get("autotune", {})
    acquisition = bundle.get("acquisition", {}).get("health", {})
    paper_real = bundle.get("acquisition", {}).get("paper_real_outcomes", {})
    moonshot = bundle.get("moonshots", {}).get("moonshot_micro_lottery", {})
    paper_summary = paper_real.get("summary") if isinstance(paper_real, dict) else {}
    return {
        "recommended_next_action": health.get("recommended_next_action") if isinstance(health, dict) else None,
        "acquisition_recommended_action": acquisition.get("recommended_action") if isinstance(acquisition, dict) else None,
        "acquisition_buys": acquisition.get("buys") if isinstance(acquisition, dict) else None,
        "actual_paper_buy_attempts": acquisition.get("actual_paper_buy_attempts") if isinstance(acquisition, dict) else None,
        "actual_paper_buys": acquisition.get("actual_paper_buys") if isinstance(acquisition, dict) else None,
        "blocked_before_buy": acquisition.get("blocked_before_buy") if isinstance(acquisition, dict) else None,
        "shadow_only": acquisition.get("shadow_only") if isinstance(acquisition, dict) else None,
        "acquisition_buys_per_hour": acquisition.get("buys_per_hour") if isinstance(acquisition, dict) else None,
        "acquisition_top_blockers": acquisition.get("top_blockers") if isinstance(acquisition, dict) else None,
        "current_run_closed_positions": current.get("closed_positions") if isinstance(current, dict) else None,
        "paper_real_closed_positions": paper_summary.get("closed") if isinstance(paper_summary, dict) else None,
        "paper_real_win_rate_pct": paper_summary.get("win_rate_pct") if isinstance(paper_summary, dict) else None,
        "paper_real_avg_pnl_pct": paper_summary.get("avg_pnl_pct") if isinstance(paper_summary, dict) else None,
        "current_run_strategy_decisions": current.get("strategy_decisions") if isinstance(current, dict) else None,
        "current_run_autotune_actions": len(autotune.get("actions") or []) if isinstance(autotune, dict) else None,
        "current_run_autotune_recommended_changes": autotune.get("recommended_changes") if isinstance(autotune, dict) else None,
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
    read_cache: dict[Path, Any] = {}
    bundle: dict[str, Any] = {
        "generated_at_utc": utc_now(),
        "current_run": _section(resolved_root, "current_run", cache=read_cache),
        "acquisition": _section(resolved_root, "acquisition", cache=read_cache),
        "historical": _section(resolved_root, "historical", cache=read_cache),
        "lanes": _section(resolved_root, "lanes", cache=read_cache),
        "exits": _section(resolved_root, "exits", cache=read_cache),
        "moonshots": _section(resolved_root, "moonshots", cache=read_cache),
        "api_budget": {},
        "funnel": _section(resolved_root, "funnel", cache=read_cache),
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
