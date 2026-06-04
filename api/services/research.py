from __future__ import annotations

from pathlib import Path
from typing import Any

from api.repositories.filesystem import file_mtime, read_json_file
from api.schemas.common import Envelope, SourceStatus
from api.services.common import build_envelope, iso_or_none
from api.services.common import make_source_status
from api.services.sources import json_status
from api.settings import APISettings

ACCEPTED_STATUSES = {"accepted_paper", "accepted_replay"}
REJECTED_STATUSES = {"rejected_paper", "rejected", "failed"}


def _research_runs_dir(settings: APISettings) -> Path:
    return settings.data_dir / "research_runs"


def _scoreboard_path(settings: APISettings) -> Path:
    return _research_runs_dir(settings) / "scoreboard.json"


def _scoreboard_md_path(settings: APISettings) -> Path:
    return _research_runs_dir(settings) / "scoreboard.md"


def _api_budget_path(settings: APISettings) -> Path:
    return _research_runs_dir(settings) / "api_budget.json"


def _current_best_path(settings: APISettings) -> Path:
    return _research_runs_dir(settings) / "current_best_policy.json"


def _runs_root(settings: APISettings) -> Path:
    return _research_runs_dir(settings) / "runs"


def _paper_forward_root(settings: APISettings) -> Path:
    return _research_runs_dir(settings) / "paper_forward"


def _metrics_path(settings: APISettings, name: str) -> Path:
    return settings.metrics_dir / name


def _proposal_path(settings: APISettings, proposal_id: str | None) -> Path | None:
    if not proposal_id:
        return None
    for folder in ("accepted_paper", "accepted_replay", "accepted", "candidates", "rejected", "failed"):
        path = settings.project_root / "strategy_proposals" / folder / f"{proposal_id}.json"
        if path.exists():
            return path
    return None


def _read_dict(path: Path) -> dict[str, Any]:
    payload = read_json_file(path)
    return payload if isinstance(payload, dict) else {}


def _read_list(path: Path) -> list[Any]:
    payload = read_json_file(path)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
        return payload["entries"]
    return []


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _updated_at(path: Path) -> str | None:
    if not path.exists():
        return None
    return iso_or_none(file_mtime(path))


def _json_status(settings: APISettings, source_key: str, path: Path, *, optional: bool = True) -> SourceStatus:
    return json_status(
        source_key=source_key,
        path=path,
        generated_field="generated_at_utc",
        optional=optional,
        empty_when_missing=optional,
    )


def _dir_status(source_key: str, path: Path, *, optional: bool = True) -> SourceStatus:
    if not path.exists():
        return make_source_status(
            source_key=source_key,
            kind="directory",
            status="empty" if optional else "missing",
            detail="directory_missing",
            path=path,
        )
    if not path.is_dir():
        return make_source_status(
            source_key=source_key,
            kind="directory",
            status="error",
            detail="not_a_directory",
            path=path,
        )
    rows = len([item for item in path.iterdir()])
    return make_source_status(
        source_key=source_key,
        kind="directory",
        status="ok" if rows else "empty",
        updated_at=file_mtime(path),
        detail=f"entries={rows}",
        path=path,
    )


def _scoreboard_entries(settings: APISettings) -> list[dict[str, Any]]:
    return [entry for entry in _read_list(_scoreboard_path(settings)) if isinstance(entry, dict)]


def _entry_sort_key(entry: dict[str, Any]) -> tuple[str, float]:
    return (str(entry.get("evaluated_at_utc") or ""), _safe_float(entry.get("objective_score"), -1e18))


def _best_entry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    accepted_paper = [entry for entry in entries if entry.get("status") == "accepted_paper"]
    accepted_replay = [entry for entry in entries if entry.get("status") == "accepted_replay"]
    pool = accepted_paper or accepted_replay or entries
    if not pool:
        return None
    return max(pool, key=lambda entry: (_safe_float(entry.get("objective_score"), -1e18), str(entry.get("evaluated_at_utc") or "")))


def _status_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        status = str(entry.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _objective_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [_safe_float(entry.get("objective_score")) for entry in entries if entry.get("objective_score") is not None]
    latest = sorted(entries, key=_entry_sort_key, reverse=True)[0] if entries else None
    return {
        "best_objective_score": max(scores) if scores else None,
        "latest_objective_score": latest.get("objective_score") if latest else None,
        "latest_status": latest.get("status") if latest else None,
        "latest_run_id": latest.get("run_id") if latest else None,
    }


def get_research_scoreboard_envelope(settings: APISettings) -> Envelope:
    entries = sorted(_scoreboard_entries(settings), key=_entry_sort_key, reverse=True)
    best = _best_entry(entries)
    accepted_count = sum(1 for entry in entries if str(entry.get("status")) in ACCEPTED_STATUSES)
    rejected_count = sum(1 for entry in entries if str(entry.get("status")) in REJECTED_STATUSES)
    data = {
        "count": len(entries),
        "summary": {
            "status_counts": _status_counts(entries),
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            **_objective_stats(entries),
            "best_run_id": best.get("run_id") if best else None,
            "best_proposal_id": best.get("proposal_id") if best else None,
            "best_status": best.get("status") if best else None,
        },
        "entries": entries,
    }
    status = _json_status(settings, "autoresearch.scoreboard", _scoreboard_path(settings))
    md_status = _json_status(settings, "autoresearch.scoreboard_md", _scoreboard_md_path(settings))
    return build_envelope(data, source_status=[status, md_status], empty=not entries)


def _run_item(settings: APISettings, run_dir: Path, scoreboard_by_run: dict[str, dict[str, Any]]) -> dict[str, Any]:
    candidate = _read_dict(run_dir / "candidate_policy.json")
    replay_metrics = _read_dict(run_dir / "replay_metrics.json")
    safety = _read_dict(run_dir / "safety_report.json")
    run_id = run_dir.name
    entry = scoreboard_by_run.get(run_id, {})
    proposal_id = str(candidate.get("proposal_id") or entry.get("proposal_id") or run_id)
    status = entry.get("status") or replay_metrics.get("status") or ("failed" if replay_metrics.get("failed") else "unknown")
    return {
        "run_id": run_id,
        "proposal_id": proposal_id,
        "status": status,
        "objective_score": entry.get("objective_score"),
        "candidate_status": entry.get("status"),
        "updated_at": _updated_at(run_dir / "replay_metrics.json") or _updated_at(run_dir / "candidate_policy.json"),
        "candidate_policy": candidate,
        "replay_metrics": replay_metrics,
        "safety": safety,
        "paths": {
            "run_dir": str(run_dir),
            "candidate_policy": str(run_dir / "candidate_policy.json"),
            "replay_metrics": str(run_dir / "replay_metrics.json"),
            "candidate_diff": str(run_dir / "candidate_diff.md"),
        },
    }


def get_research_runs_envelope(settings: APISettings, *, limit: int = 50) -> Envelope:
    entries = _scoreboard_entries(settings)
    scoreboard_by_run = {str(entry.get("run_id") or ""): entry for entry in entries}
    run_dirs = [path for path in _runs_root(settings).iterdir() if path.is_dir()] if _runs_root(settings).exists() else []
    items = [_run_item(settings, run_dir, scoreboard_by_run) for run_dir in run_dirs]
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    data = {
        "count": len(items),
        "items": items[: max(1, min(int(limit), 250))],
        "status_counts": _status_counts([item for item in items if isinstance(item, dict)]),
    }
    statuses = [
        _json_status(settings, "autoresearch.scoreboard", _scoreboard_path(settings)),
        _dir_status("autoresearch.runs_dir", _runs_root(settings), optional=True),
    ]
    return build_envelope(data, source_status=statuses, empty=not items)


def get_research_current_best_envelope(settings: APISettings) -> Envelope:
    persisted = _read_dict(_current_best_path(settings))
    entries = _scoreboard_entries(settings)
    best = _best_entry(entries)
    proposal_id = str((persisted.get("proposal_id") if persisted else None) or (best or {}).get("proposal_id") or "")
    run_id = str((persisted.get("run_id") if persisted else None) or (best or {}).get("run_id") or "")
    run_candidate = _read_dict(_runs_root(settings) / run_id / "candidate_policy.json") if run_id else {}
    proposal_path = _proposal_path(settings, proposal_id)
    proposal = _read_dict(proposal_path) if proposal_path is not None else {}
    candidate_policy = persisted.get("candidate_policy") if isinstance(persisted.get("candidate_policy"), dict) else {}
    if not candidate_policy:
        candidate_policy = run_candidate or proposal
    source = "current_best_policy" if persisted else "scoreboard"
    data = {
        "source": source if best or persisted else "none",
        "entry": best,
        "run_id": run_id or None,
        "proposal_id": proposal_id or None,
        "status": (best or {}).get("status") or persisted.get("status"),
        "objective_score": (best or {}).get("objective_score") or persisted.get("objective_score"),
        "candidate_policy": candidate_policy or None,
        "proposal_path": str(proposal_path) if proposal_path else None,
    }
    statuses = [
        _json_status(settings, "autoresearch.current_best_policy", _current_best_path(settings)),
        _json_status(settings, "autoresearch.scoreboard", _scoreboard_path(settings)),
    ]
    return build_envelope(data, source_status=statuses, empty=not bool(best or persisted))


def _api_budget_health(payload: dict[str, Any]) -> dict[str, Any]:
    api_429_count = (
        _safe_int(payload.get("gecko_429_count"))
        + _safe_int(payload.get("birdeye_429_count"))
        + _safe_int(payload.get("jupiter_rate_limit_count"))
    )
    degraded_minutes = _safe_int(payload.get("provider_degraded_minutes"))
    status = "ok" if api_429_count == 0 and degraded_minutes == 0 else "warn"
    return {
        "status": status,
        "api_429_count": api_429_count,
        "provider_degraded_minutes": degraded_minutes,
        "cooldown_count": _safe_int(payload.get("cooldown_count")),
        "rpc_errors": _safe_int(payload.get("rpc_errors")),
    }


def get_research_api_budget_envelope(settings: APISettings) -> Envelope:
    payload = _read_dict(_api_budget_path(settings))
    metrics_payload = _read_dict(_metrics_path(settings, "api_budget_report.json"))
    data = {
        "summary": _api_budget_health(payload),
        "api_budget": payload,
        "metrics_report": metrics_payload,
    }
    statuses = [
        _json_status(settings, "autoresearch.api_budget", _api_budget_path(settings)),
        _json_status(settings, "metrics.api_budget_report", _metrics_path(settings, "api_budget_report.json")),
    ]
    return build_envelope(data, source_status=statuses, empty=not bool(payload or metrics_payload))


def get_research_moonshot_progress_envelope(settings: APISettings) -> Envelope:
    moonshot = _read_dict(_metrics_path(settings, "moonshot_micro_lottery_report.json"))
    runner = _read_dict(_metrics_path(settings, "runner_capture_ladder_report.json"))
    missed = _read_dict(_metrics_path(settings, "missed_pumps.json"))
    summary = {
        "moonshot_candidates_seen": moonshot.get("candidates_seen") or moonshot.get("rows"),
        "moonshot_buys": moonshot.get("buys") or moonshot.get("paper_buys"),
        "moonshot_peak100_capture": moonshot.get("peak100_capture") or moonshot.get("moonshot_peak100_capture"),
        "moonshot_peak500_capture": moonshot.get("peak500_capture") or moonshot.get("moonshot_peak500_capture"),
        "runner_capture_ratio": runner.get("runner_capture_ratio") or runner.get("capture_ratio"),
        "missed_peak100_count": missed.get("missed_peak100_count"),
        "missed_peak500_count": missed.get("missed_peak500_count"),
        "missed_peak1000_count": missed.get("missed_peak1000_count"),
    }
    data = {
        "summary": summary,
        "moonshot_micro_lottery": moonshot,
        "runner_capture_ladder": runner,
        "missed_pumps": missed,
    }
    statuses = [
        _json_status(settings, "metrics.moonshot_micro_lottery", _metrics_path(settings, "moonshot_micro_lottery_report.json")),
        _json_status(settings, "metrics.runner_capture_ladder", _metrics_path(settings, "runner_capture_ladder_report.json")),
        _json_status(settings, "metrics.missed_pumps", _metrics_path(settings, "missed_pumps.json")),
    ]
    return build_envelope(data, source_status=statuses, empty=not bool(moonshot or runner or missed))


def _paper_run_item(run_dir: Path) -> dict[str, Any]:
    state = _read_dict(run_dir / "paper_forward_state.json")
    result = _read_dict(run_dir / "paper_forward_result.json")
    rollback = _read_dict(run_dir / "rollback_report.json")
    demotion = _read_dict(run_dir / "demotion_report.json")
    status = result.get("status") or state.get("status") or demotion.get("status") or "unknown"
    return {
        "run_id": state.get("run_id") or result.get("run_id") or run_dir.name,
        "status": status,
        "accepted": result.get("accepted"),
        "started_at_utc": state.get("started_at_utc"),
        "finalized_at_utc": state.get("finalized_at_utc"),
        "paper_profile": state.get("paper_profile"),
        "proposal_id": (state.get("promotion") or {}).get("proposal_id") if isinstance(state.get("promotion"), dict) else None,
        "objective_score": (result.get("objective") or {}).get("score") if isinstance(result.get("objective"), dict) else None,
        "rejection_reasons": result.get("rejection_reasons") or state.get("rejection_reasons") or [],
        "warnings": result.get("warnings") or [],
        "state": state,
        "result": result,
        "rollback": rollback,
        "demotion": demotion,
        "paths": {
            "run_dir": str(run_dir),
            "state": str(run_dir / "paper_forward_state.json"),
            "result": str(run_dir / "paper_forward_result.json"),
            "rollback": str(run_dir / "rollback_report.json"),
            "demotion": str(run_dir / "demotion_report.json"),
        },
    }


def get_research_paper_forward_envelope(settings: APISettings, *, limit: int = 25) -> Envelope:
    root = _paper_forward_root(settings)
    run_dirs = [path for path in root.iterdir() if path.is_dir()] if root.exists() else []
    items = [_paper_run_item(run_dir) for run_dir in run_dirs]
    items.sort(key=lambda item: str(item.get("finalized_at_utc") or item.get("started_at_utc") or ""), reverse=True)
    demotion_latest = _read_dict(root / "demotion_latest.json")
    latest = items[0] if items else None
    data = {
        "count": len(items),
        "latest": latest,
        "active": [item for item in items if item.get("status") == "paper_forward_started"],
        "status_counts": _status_counts([item for item in items if isinstance(item, dict)]),
        "items": items[: max(1, min(int(limit), 100))],
        "demotion_latest": demotion_latest or None,
    }
    statuses = [
        _dir_status("autoresearch.paper_forward_dir", root, optional=True),
        _json_status(settings, "autoresearch.paper_demotion_latest", root / "demotion_latest.json"),
    ]
    return build_envelope(data, source_status=statuses, empty=not items)


__all__ = [
    "get_research_api_budget_envelope",
    "get_research_current_best_envelope",
    "get_research_moonshot_progress_envelope",
    "get_research_paper_forward_envelope",
    "get_research_runs_envelope",
    "get_research_scoreboard_envelope",
]
