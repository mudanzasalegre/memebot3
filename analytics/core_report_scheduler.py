from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable

from analytics.report_utils import metrics_dir, write_json
from config.config import PROJECT_ROOT


REQUIRED_CORE_REPORTS = (
    "trade_diagnostics.json",
    "policy_replay.json",
    "missed_pumps.json",
    "post_hotfix_strategy_preview.json",
    "runner_capture_ladder_report.json",
    "untagged_buy_block_report.json",
    "sniper_research_subprofile_report.json",
    "pumpswap_rebound_confirmation_report.json",
    "research_rank_canary_audit.json",
    "runner_turbo_monitor_report.json",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def _write_placeholder(path: Path, *, warning: str | None = None) -> dict[str, Any]:
    payload = {
        "generated_at_utc": _utc_now(),
        "placeholder": True,
        "warning": warning or "no_data_or_generator_unavailable",
        "rows": 0,
    }
    write_json(path, payload)
    return payload


def _ensure_generated_at(path: Path) -> None:
    payload = _read_json(path)
    if payload is None:
        _write_placeholder(path, warning="report_unreadable_after_generation")
        return
    if isinstance(payload, dict):
        if "generated_at_utc" not in payload:
            payload = {"generated_at_utc": _utc_now(), **payload}
            write_json(path, payload)
        return
    wrapped = {
        "generated_at_utc": _utc_now(),
        "rows": len(payload) if isinstance(payload, list) else 0,
        "data": payload,
    }
    write_json(path, wrapped)


def report_freshness(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    out: dict[str, Any] = {
        "generated_at_utc": _utc_now(),
        "missing": [],
        "reports": {},
    }
    for name in REQUIRED_CORE_REPORTS:
        path = metrics_dir(root) / name
        payload = _read_json(path)
        generated = payload.get("generated_at_utc") if isinstance(payload, dict) else None
        exists = path.exists()
        if not exists:
            out["missing"].append(name)
        out["reports"][name] = {
            "path": str(path),
            "exists": exists,
            "generated_at_utc": generated,
            "mtime_utc": dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat()
            if exists
            else None,
        }
    return out


def _generators(root: Path) -> dict[str, Callable[[], Any]]:
    from analytics.missed_pumps import write_missed_pumps_report
    from analytics.post_hotfix_strategy_preview import write_post_hotfix_strategy_preview
    from analytics.runner_capture_ladder_report import write_runner_capture_ladder_report
    from analytics.runner_turbo_monitor import write_runner_turbo_monitor_report
    from analytics.pumpswap_rebound_prime import write_pumpswap_rebound_confirmation_report
    from analytics.research_rank_canary import write_research_rank_canary_audit_report
    from analytics.sniper_research_subprofiles import write_sniper_research_subprofile_report
    from analytics.trade_diagnostics import write_trade_diagnostics_report
    from analytics.untagged_buy_block import write_untagged_buy_block_report
    from backtest.policy_replay import write_policy_replay

    return {
        "trade_diagnostics.json": lambda: write_trade_diagnostics_report(root),
        "policy_replay.json": lambda: write_policy_replay(root),
        "missed_pumps.json": lambda: write_missed_pumps_report(root),
        "post_hotfix_strategy_preview.json": lambda: write_post_hotfix_strategy_preview(root),
        "runner_capture_ladder_report.json": lambda: write_runner_capture_ladder_report(root),
        "untagged_buy_block_report.json": lambda: write_untagged_buy_block_report(root),
        "sniper_research_subprofile_report.json": lambda: write_sniper_research_subprofile_report(root),
        "pumpswap_rebound_confirmation_report.json": lambda: write_pumpswap_rebound_confirmation_report(root),
        "research_rank_canary_audit.json": lambda: write_research_rank_canary_audit_report(root),
        "runner_turbo_monitor_report.json": lambda: write_runner_turbo_monitor_report(root),
    }


def regenerate_core_reports(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    target_dir = metrics_dir(root)
    target_dir.mkdir(parents=True, exist_ok=True)
    generated: dict[str, Any] = {}
    warnings: dict[str, str] = {}
    generators = _generators(root)
    for name in REQUIRED_CORE_REPORTS:
        path = target_dir / name
        try:
            generator = generators.get(name)
            if generator is None:
                _write_placeholder(path, warning="generator_missing")
                warnings[name] = "generator_missing"
            else:
                generator()
                if not path.exists():
                    _write_placeholder(path, warning="generator_did_not_create_file")
                    warnings[name] = "generator_did_not_create_file"
            _ensure_generated_at(path)
            generated[name] = {
                "path": str(path),
                "exists": path.exists(),
            }
        except Exception as exc:
            warnings[name] = str(exc)
            _write_placeholder(path, warning=str(exc))
            generated[name] = {
                "path": str(path),
                "exists": path.exists(),
                "placeholder": True,
            }
    freshness = report_freshness(root)
    summary = {
        "generated_at_utc": _utc_now(),
        "reports": generated,
        "warnings": warnings,
        "freshness": freshness,
    }
    write_json(target_dir / "core_reports_regeneration.json", summary)
    return summary


def ensure_core_report_placeholders(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    created: list[str] = []
    for name in REQUIRED_CORE_REPORTS:
        path = metrics_dir(root) / name
        if not path.exists():
            _write_placeholder(path, warning="created_on_startup_missing_report")
            created.append(name)
    return {
        "generated_at_utc": _utc_now(),
        "created": created,
        "freshness": report_freshness(root),
    }


__all__ = [
    "REQUIRED_CORE_REPORTS",
    "ensure_core_report_placeholders",
    "regenerate_core_reports",
    "report_freshness",
]
