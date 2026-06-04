from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from research_loop.evaluator import EvaluationResult, evaluate_replay_run
from research_loop.paths import project_root, research_runs_dir

SCOREBOARD_JSON = "scoreboard.json"
SCOREBOARD_MD = "scoreboard.md"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


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


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _load_candidate_policy(candidate_policy: str | Path | dict[str, Any] | None) -> dict[str, Any]:
    if candidate_policy is None:
        return {}
    if isinstance(candidate_policy, dict):
        return dict(candidate_policy)
    payload = _read_json(Path(candidate_policy))
    return payload if isinstance(payload, dict) else {}


def _delta(deltas: dict[str, float], key: str) -> float:
    try:
        return float(deltas.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _api_budget_delta(deltas: dict[str, float]) -> dict[str, float]:
    return {
        "api_429_count_delta": _delta(deltas, "api_429_count"),
        "provider_degraded_minutes_delta": _delta(deltas, "provider_degraded_minutes"),
        "gecko_429_count_delta": _delta(deltas, "gecko_429_count"),
        "birdeye_429_count_delta": _delta(deltas, "birdeye_429_count"),
        "jupiter_rate_limit_count_delta": _delta(deltas, "jupiter_rate_limit_count"),
    }


def build_scoreboard_entry(
    *,
    run_id: str,
    candidate_policy: str | Path | dict[str, Any] | None,
    evaluation_result: EvaluationResult,
    created_at_utc: str | None = None,
    evaluated_at_utc: str | None = None,
) -> dict[str, Any]:
    policy = _load_candidate_policy(candidate_policy)
    objective = evaluation_result.objective
    deltas = objective.metric_deltas if objective is not None else {}
    proposal_id = evaluation_result.proposal_id or str(policy.get("proposal_id") or "")
    moonshot_delta = (
        _delta(deltas, "moonshot_peak100_capture")
        + _delta(deltas, "moonshot_peak500_capture")
        + _delta(deltas, "moonshot_peak1000_capture")
    )
    return {
        "run_id": run_id,
        "proposal_id": proposal_id,
        "status": evaluation_result.status,
        "objective_score": objective.score if objective is not None else None,
        "total_pnl_delta": _delta(deltas, "total_pnl_usd"),
        "avg_pnl_delta": _delta(deltas, "avg_pnl_pct"),
        "median_pnl_delta": _delta(deltas, "median_pnl_pct"),
        "win_rate_delta": _delta(deltas, "win_rate_pct"),
        "runner_capture_delta": _delta(deltas, "runner_capture_ratio"),
        "moonshot_capture_delta": moonshot_delta,
        "severe_loss_delta": _delta(deltas, "severe_loss_count"),
        "liquidity_crush_delta": _delta(deltas, "liquidity_crush_count"),
        "adverse_tick_delta": _delta(deltas, "adverse_tick_count"),
        "api_budget_delta": _api_budget_delta(deltas),
        "created_at_utc": created_at_utc or str(policy.get("created_at_utc") or ""),
        "evaluated_at_utc": evaluated_at_utc or utc_now(),
        "rejection_reasons": list(evaluation_result.rejection_reasons),
        "warnings": list(evaluation_result.warnings),
    }


def load_scoreboard(root: str | Path | None = None) -> list[dict[str, Any]]:
    path = research_runs_dir(project_root(root)) / SCOREBOARD_JSON
    payload = _read_json(path)
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        entries = payload.get("entries")
        if isinstance(entries, list):
            return [row for row in entries if isinstance(row, dict)]
    return []


def _entry_key(entry: dict[str, Any]) -> tuple[str, str]:
    return (str(entry.get("run_id") or ""), str(entry.get("proposal_id") or ""))


def upsert_scoreboard_entry(
    entry: dict[str, Any],
    *,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    entries = load_scoreboard(root)
    target_key = _entry_key(entry)
    replaced = False
    updated: list[dict[str, Any]] = []
    for existing in entries:
        if _entry_key(existing) == target_key:
            updated.append(entry)
            replaced = True
        else:
            updated.append(existing)
    if not replaced:
        updated.append(entry)
    write_scoreboard(updated, root=root)
    return updated


def _format_float(value: Any, digits: int = 3) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def render_scoreboard_markdown(entries: list[dict[str, Any]]) -> str:
    lines = [
        "# AutoResearch Scoreboard",
        "",
        "| Run | Proposal | Status | Score | PnL d | Median d | Win d | Runner d | API 429 d | Rejections |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for entry in sorted(entries, key=lambda row: str(row.get("evaluated_at_utc") or ""), reverse=True):
        api_delta = entry.get("api_budget_delta") if isinstance(entry.get("api_budget_delta"), dict) else {}
        rejection_reasons = entry.get("rejection_reasons") or []
        rejections = ", ".join(str(reason) for reason in rejection_reasons) if rejection_reasons else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{entry.get('run_id') or ''}`",
                    f"`{entry.get('proposal_id') or ''}`",
                    f"`{entry.get('status') or ''}`",
                    _format_float(entry.get("objective_score")),
                    _format_float(entry.get("total_pnl_delta")),
                    _format_float(entry.get("median_pnl_delta")),
                    _format_float(entry.get("win_rate_delta")),
                    _format_float(entry.get("runner_capture_delta")),
                    _format_float(api_delta.get("api_429_count_delta") if isinstance(api_delta, dict) else 0.0),
                    rejections,
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def write_scoreboard(entries: list[dict[str, Any]], *, root: str | Path | None = None) -> dict[str, Any]:
    resolved_root = project_root(root)
    payload = {
        "generated_at_utc": utc_now(),
        "entries": entries,
    }
    output_dir = research_runs_dir(resolved_root)
    _write_json(output_dir / SCOREBOARD_JSON, payload)
    _write_text(output_dir / SCOREBOARD_MD, render_scoreboard_markdown(entries))
    return payload


def record_evaluation(
    *,
    run_id: str,
    candidate_policy: str | Path | dict[str, Any] | None,
    evaluation_result: EvaluationResult,
    root: str | Path | None = None,
) -> dict[str, Any]:
    entry = build_scoreboard_entry(
        run_id=run_id,
        candidate_policy=candidate_policy,
        evaluation_result=evaluation_result,
    )
    upsert_scoreboard_entry(entry, root=root)
    return entry


def record_run_evaluation(
    run_dir: str | Path,
    baseline_metrics: dict[str, Any] | str | Path | None = None,
    *,
    root: str | Path | None = None,
    min_closed_trades: int = 0,
) -> dict[str, Any]:
    result = evaluate_replay_run(
        run_dir,
        baseline_metrics,
        min_closed_trades=min_closed_trades,
    )
    run_path = Path(run_dir)
    return record_evaluation(
        run_id=run_path.name,
        candidate_policy=run_path / "candidate_policy.json",
        evaluation_result=result,
        root=root,
    )


__all__ = [
    "SCOREBOARD_JSON",
    "SCOREBOARD_MD",
    "build_scoreboard_entry",
    "load_scoreboard",
    "record_evaluation",
    "record_run_evaluation",
    "render_scoreboard_markdown",
    "upsert_scoreboard_entry",
    "write_scoreboard",
]
