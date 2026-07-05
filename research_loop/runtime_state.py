from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Mapping

from research_loop.paths import project_root, research_runs_dir

RUNTIME_STATE_NAME = "autoresearch_runtime_state.json"
EVENTS_NAME = "autoresearch_events.jsonl"
EVENT_AUTORESEARCH_START = "AUTORESEARCH_START"
EVENT_AUTORESEARCH_STOP = "AUTORESEARCH_STOP"
EVENT_AUTORESEARCH_CYCLE_START = "AUTORESEARCH_CYCLE_START"
EVENT_AUTORESEARCH_REPORT_BUNDLE_READY = "AUTORESEARCH_REPORT_BUNDLE_READY"
EVENT_AUTORESEARCH_API_BUDGET_READY = "AUTORESEARCH_API_BUDGET_READY"
EVENT_AUTORESEARCH_CANDIDATES_GENERATED = "AUTORESEARCH_CANDIDATES_GENERATED"
EVENT_AUTORESEARCH_REPLAY_START = "AUTORESEARCH_REPLAY_START"
EVENT_AUTORESEARCH_REPLAY_DONE = "AUTORESEARCH_REPLAY_DONE"
EVENT_AUTORESEARCH_CANDIDATE_ACCEPTED = "AUTORESEARCH_CANDIDATE_ACCEPTED"
EVENT_AUTORESEARCH_CANDIDATE_REJECTED = "AUTORESEARCH_CANDIDATE_REJECTED"
EVENT_AUTORESEARCH_CANDIDATE_FAILED = "AUTORESEARCH_CANDIDATE_FAILED"
EVENT_AUTORESEARCH_SCOREBOARD_UPDATED = "AUTORESEARCH_SCOREBOARD_UPDATED"
EVENT_AUTORESEARCH_PAPER_PROMOTION_SKIPPED = "AUTORESEARCH_PAPER_PROMOTION_SKIPPED"
EVENT_AUTORESEARCH_PAPER_PROMOTION_CREATED = "AUTORESEARCH_PAPER_PROMOTION_CREATED"
EVENT_AUTORESEARCH_CYCLE_END = "AUTORESEARCH_CYCLE_END"
EVENT_AUTORESEARCH_ERROR = "AUTORESEARCH_ERROR"
MANDATORY_AUTORESEARCH_EVENTS = (
    EVENT_AUTORESEARCH_START,
    EVENT_AUTORESEARCH_STOP,
    EVENT_AUTORESEARCH_CYCLE_START,
    EVENT_AUTORESEARCH_REPORT_BUNDLE_READY,
    EVENT_AUTORESEARCH_API_BUDGET_READY,
    EVENT_AUTORESEARCH_CANDIDATES_GENERATED,
    EVENT_AUTORESEARCH_REPLAY_START,
    EVENT_AUTORESEARCH_REPLAY_DONE,
    EVENT_AUTORESEARCH_CANDIDATE_ACCEPTED,
    EVENT_AUTORESEARCH_CANDIDATE_REJECTED,
    EVENT_AUTORESEARCH_CANDIDATE_FAILED,
    EVENT_AUTORESEARCH_SCOREBOARD_UPDATED,
    EVENT_AUTORESEARCH_PAPER_PROMOTION_SKIPPED,
    EVENT_AUTORESEARCH_PAPER_PROMOTION_CREATED,
    EVENT_AUTORESEARCH_CYCLE_END,
    EVENT_AUTORESEARCH_ERROR,
)

ACCEPTED_CANDIDATE_STATUSES = {"accepted_replay", "accepted_paper"}
REJECTED_CANDIDATE_STATUSES = {"rejected", "rejected_paper"}
FAILED_CANDIDATE_STATUSES = {"failed"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def runtime_state_path(root: str | Path | None = None) -> Path:
    return research_runs_dir(project_root(root)) / RUNTIME_STATE_NAME


def events_path(root: str | Path | None = None) -> Path:
    return research_runs_dir(project_root(root)) / EVENTS_NAME


def read_runtime_state(root: str | Path | None = None) -> dict[str, Any]:
    path = runtime_state_path(root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_events(root: str | Path | None = None) -> list[dict[str, Any]]:
    path = events_path(root)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def write_runtime_state(root: str | Path | None, state: Mapping[str, Any]) -> Path:
    path = runtime_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(state), indent=2, sort_keys=True, default=str), encoding="utf-8")
    return path


def update_runtime_state(root: str | Path | None, updates: Mapping[str, Any]) -> dict[str, Any]:
    state = read_runtime_state(root)
    state.update(dict(updates))
    write_runtime_state(root, state)
    return state


def append_event(
    root: str | Path | None,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
) -> Path:
    path = events_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "event": event_type,
        "timestamp_utc": utc_now(),
    }
    if payload:
        event.update(dict(payload))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, default=str))
        handle.write("\n")
    return path


def _config_value(config: Any, name: str, default: Any) -> Any:
    if isinstance(config, Mapping):
        return config.get(name, default)
    return getattr(config, name, default)


def initial_runtime_state(config: Any, *, started_at_utc: str | None = None) -> dict[str, Any]:
    started = started_at_utc or utc_now()
    return {
        "enabled": bool(_config_value(config, "enabled", True)),
        "mode": str(_config_value(config, "mode", "paper_replay")),
        "started_at_utc": started,
        "last_cycle_at_utc": None,
        "next_cycle_at_utc": None,
        "last_status": "starting",
        "last_error": None,
        "cycles_completed": 0,
        "candidates_generated_total": 0,
        "candidates_accepted_total": 0,
        "candidates_rejected_total": 0,
        "current_best_policy": None,
        "live_promotion_enabled": bool(_config_value(config, "live_promotion_enabled", False)),
        "auto_live_promote": bool(_config_value(config, "auto_live_promote", False)),
    }


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "as_dict"):
        payload = value.as_dict()
        return payload if isinstance(payload, dict) else {}
    return {}


def _result_status(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("status") or "")
    return str(getattr(result, "status", "") or "")


def _result_field(result: Any, key: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def _batch_results(batch: Any) -> list[Any]:
    if isinstance(batch, dict):
        results = batch.get("results")
    else:
        results = getattr(batch, "results", None)
    return results if isinstance(results, list) else []


def summarize_cycle_result(cycle_result: Any) -> dict[str, Any]:
    payload = _as_dict(cycle_result)
    generated = 0
    accepted = 0
    rejected = 0
    failed = 0
    best_policy: dict[str, Any] | None = None
    best_score = float("-inf")

    for batch in payload.get("batches") or []:
        batch_payload = _as_dict(batch)
        try:
            generated += int(batch_payload.get("candidates_generated") or 0)
        except (TypeError, ValueError):
            generated += 0
        for result in _batch_results(batch):
            status = _result_status(result)
            if status in ACCEPTED_CANDIDATE_STATUSES:
                accepted += 1
                try:
                    score = float(_result_field(result, "objective_score", 0.0) or 0.0)
                except (TypeError, ValueError):
                    score = 0.0
                if score >= best_score:
                    best_score = score
                    best_policy = {
                        "proposal_id": _result_field(result, "proposal_id"),
                        "run_id": _result_field(result, "run_id"),
                        "status": status,
                        "objective_score": _result_field(result, "objective_score"),
                    }
            elif status in REJECTED_CANDIDATE_STATUSES:
                rejected += 1
            elif status in FAILED_CANDIDATE_STATUSES:
                failed += 1

    return {
        "candidates_generated": generated,
        "candidates_accepted": accepted,
        "candidates_rejected": rejected,
        "candidates_failed": failed,
        "current_best_policy": best_policy,
    }


def record_runtime_start(
    root: str | Path | None,
    config: Any,
    *,
    once: bool,
    interval_hours: float,
) -> dict[str, Any]:
    state = initial_runtime_state(config)
    write_runtime_state(root, state)
    append_event(
        root,
        EVENT_AUTORESEARCH_START,
        {
            "mode": state["mode"],
            "once": once,
            "interval_hours": interval_hours,
            "live_promotion_enabled": state["live_promotion_enabled"],
            "auto_live_promote": state["auto_live_promote"],
        },
    )
    return state


def record_cycle_completion(
    root: str | Path | None,
    config: Any,
    cycle_result: Any,
    *,
    next_cycle_at_utc: str | None,
) -> dict[str, Any]:
    state = read_runtime_state(root) or initial_runtime_state(config)
    summary = summarize_cycle_result(cycle_result)
    payload = _as_dict(cycle_result)
    failures = payload.get("failures") if isinstance(payload.get("failures"), list) else []
    best_policy = summary["current_best_policy"] or state.get("current_best_policy")
    updates = {
        "enabled": bool(_config_value(config, "enabled", True)),
        "mode": str(_config_value(config, "mode", "paper_replay")),
        "last_cycle_at_utc": utc_now(),
        "next_cycle_at_utc": next_cycle_at_utc,
        "last_status": str(payload.get("status") or "unknown"),
        "last_error": ";".join(str(failure) for failure in failures) if failures else None,
        "cycles_completed": int(state.get("cycles_completed") or 0) + 1,
        "candidates_generated_total": int(state.get("candidates_generated_total") or 0)
        + int(summary["candidates_generated"]),
        "candidates_accepted_total": int(state.get("candidates_accepted_total") or 0)
        + int(summary["candidates_accepted"]),
        "candidates_rejected_total": int(state.get("candidates_rejected_total") or 0)
        + int(summary["candidates_rejected"]),
        "current_best_policy": best_policy,
        "live_promotion_enabled": bool(_config_value(config, "live_promotion_enabled", False)),
        "auto_live_promote": bool(_config_value(config, "auto_live_promote", False)),
    }
    return update_runtime_state(root, updates)


def record_cycle_start(
    root: str | Path | None,
    config: Any,
    *,
    next_cycle_at_utc: str | None = None,
) -> dict[str, Any]:
    state = read_runtime_state(root) or initial_runtime_state(config)
    updates = {
        "enabled": bool(_config_value(config, "enabled", True)),
        "mode": str(_config_value(config, "mode", "paper_replay")),
        "cycle_started_at_utc": utc_now(),
        "next_cycle_at_utc": next_cycle_at_utc,
        "last_status": "cycle_running",
        "last_error": None,
        "live_promotion_enabled": bool(_config_value(config, "live_promotion_enabled", False)),
        "auto_live_promote": bool(_config_value(config, "auto_live_promote", False)),
    }
    return update_runtime_state(root, updates)


def record_runtime_error(root: str | Path | None, error: str) -> dict[str, Any]:
    return update_runtime_state(root, {"last_status": "error", "last_error": error})


__all__ = [
    "ACCEPTED_CANDIDATE_STATUSES",
    "EVENTS_NAME",
    "EVENT_AUTORESEARCH_API_BUDGET_READY",
    "EVENT_AUTORESEARCH_CANDIDATES_GENERATED",
    "EVENT_AUTORESEARCH_CANDIDATE_ACCEPTED",
    "EVENT_AUTORESEARCH_CANDIDATE_FAILED",
    "EVENT_AUTORESEARCH_CANDIDATE_REJECTED",
    "EVENT_AUTORESEARCH_CYCLE_END",
    "EVENT_AUTORESEARCH_CYCLE_START",
    "EVENT_AUTORESEARCH_ERROR",
    "EVENT_AUTORESEARCH_PAPER_PROMOTION_CREATED",
    "EVENT_AUTORESEARCH_PAPER_PROMOTION_SKIPPED",
    "EVENT_AUTORESEARCH_REPLAY_DONE",
    "EVENT_AUTORESEARCH_REPLAY_START",
    "EVENT_AUTORESEARCH_REPORT_BUNDLE_READY",
    "EVENT_AUTORESEARCH_SCOREBOARD_UPDATED",
    "EVENT_AUTORESEARCH_START",
    "EVENT_AUTORESEARCH_STOP",
    "FAILED_CANDIDATE_STATUSES",
    "MANDATORY_AUTORESEARCH_EVENTS",
    "REJECTED_CANDIDATE_STATUSES",
    "RUNTIME_STATE_NAME",
    "append_event",
    "events_path",
    "initial_runtime_state",
    "read_runtime_state",
    "read_events",
    "record_cycle_completion",
    "record_cycle_start",
    "record_runtime_error",
    "record_runtime_start",
    "runtime_state_path",
    "summarize_cycle_result",
    "update_runtime_state",
    "utc_now",
    "write_runtime_state",
]
