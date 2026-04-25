from __future__ import annotations

import datetime as dt
from typing import Any

from api.schemas.common import Envelope, SourceStatus
from api.services.common import build_envelope, make_source_status, utc_now
from api.services.runtime import (
    DEFAULT_BOT_ID,
    get_runtime_snapshot,
    runtime_snapshot_freshness,
)
from api.settings import APISettings
from runtime.process_manager import (
    clear_managed_bot_state,
    is_pid_running,
    load_managed_bot_state,
    start_managed_bot_process,
    stop_managed_bot_process,
)


BOT_START_GRACE_SECONDS = 75


def _int_or_none(value: Any) -> int | None:
    try:
        normalized = int(value)
    except Exception:
        return None
    return normalized if normalized > 0 else None


def _parse_iso(value: Any) -> dt.datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _managed_state_or_none(settings: APISettings) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    managed_state = load_managed_bot_state(settings.bot_process_state_path)
    if not isinstance(managed_state, dict):
        return None, None
    pid = _int_or_none(managed_state.get("pid"))
    if pid and is_pid_running(pid):
        return managed_state, None
    clear_managed_bot_state(settings.bot_process_state_path)
    return None, managed_state


def get_bot_process_snapshot(
    settings: APISettings,
    *,
    snapshot: dict[str, Any] | None = None,
    bot_id: str = DEFAULT_BOT_ID,
) -> tuple[dict[str, Any], SourceStatus]:
    runtime_snapshot = snapshot if snapshot is not None else get_runtime_snapshot(settings, bot_id=bot_id)
    runtime_build = (runtime_snapshot or {}).get("build_info_json") or {}
    runtime_pid = _int_or_none(runtime_build.get("pid"))
    runtime_freshness = runtime_snapshot_freshness(runtime_snapshot)
    runtime_alive = bool(runtime_pid and is_pid_running(runtime_pid))
    managed_state, dead_managed_state = _managed_state_or_none(settings)
    managed_pid = _int_or_none((managed_state or {}).get("pid"))
    managed_alive = bool(managed_pid and is_pid_running(managed_pid))
    now = utc_now()
    started_at = _parse_iso((managed_state or {}).get("started_at"))
    started_age_s = int(max(0.0, (now - started_at).total_seconds())) if started_at else None

    status = "stopped"
    detail = "No UI-managed or external bot process is active."

    if managed_state and managed_alive:
        if runtime_freshness == "fresh" and runtime_pid == managed_pid:
            status = "running_managed"
            detail = "UI-managed bot is running and publishing fresh runtime heartbeats."
        elif started_age_s is not None and started_age_s <= BOT_START_GRACE_SECONDS:
            status = "starting"
            detail = "UI-managed bot process is up and waiting for its first fresh runtime heartbeat."
        else:
            status = "running_managed"
            detail = "UI-managed bot process is alive, but the runtime heartbeat is not fresh."
    elif runtime_alive and runtime_freshness in {"fresh", "stale", "degraded"}:
        status = "running_external"
        detail = "Bot runtime appears to be active from an external console or unmanaged process."
    elif dead_managed_state:
        dead_pid = _int_or_none(dead_managed_state.get("pid"))
        status = "crashed"
        detail = f"Last UI-managed bot pid={dead_pid or 'n/a'} is no longer alive."

    payload = {
        "bot_id": bot_id,
        "status": status,
        "detail": detail,
        "managed": status in {"starting", "running_managed"},
        "external": status == "running_external",
        "can_start": status in {"stopped", "crashed"},
        "can_stop": status in {"starting", "running_managed"},
        "pid": managed_pid if managed_pid else runtime_pid,
        "managed_pid": managed_pid,
        "runtime_pid": runtime_pid,
        "runtime_freshness": runtime_freshness,
        "runtime_heartbeat_at": (runtime_snapshot or {}).get("heartbeat_at"),
        "runtime_updated_at": (runtime_snapshot or {}).get("updated_at"),
        "runtime_process_state": (runtime_snapshot or {}).get("process_state"),
        "state_file_path": str(settings.bot_process_state_path),
        "console_log_path": (managed_state or {}).get("console_log_path") or str(settings.bot_process_console_log_path),
        "started_at": (managed_state or {}).get("started_at") or (runtime_snapshot or {}).get("started_at"),
        "started_by": (managed_state or {}).get("started_by"),
        "requested_from": (managed_state or {}).get("requested_from"),
        "dry_run": (managed_state or {}).get("dry_run", (runtime_snapshot or {}).get("dry_run")),
        "file_log": (managed_state or {}).get("file_log"),
        "command": (managed_state or {}).get("command") or [],
        "startup_grace_s": BOT_START_GRACE_SECONDS,
    }

    source_status = make_source_status(
        source_key="runtime.bot_process_manager",
        kind="process",
        status=(
            "ok"
            if status in {"starting", "running_managed", "running_external"}
            else "error"
            if status == "crashed"
            else "empty"
        ),
        updated_at=payload["runtime_updated_at"] or payload["started_at"],
        detail=detail,
        path=settings.bot_process_state_path,
    )
    return payload, source_status


def get_bot_process_envelope(settings: APISettings, *, bot_id: str = DEFAULT_BOT_ID) -> Envelope:
    payload, source_status = get_bot_process_snapshot(settings, bot_id=bot_id)
    degraded = payload["status"] == "crashed"
    empty = payload["status"] == "stopped"
    return build_envelope(payload, source_status=[source_status], degraded=degraded, empty=empty, stale=False)


def start_bot_process_envelope(
    settings: APISettings,
    *,
    requested_by: str,
    requested_from: str = "ui",
    bot_id: str = DEFAULT_BOT_ID,
    dry_run: bool = True,
    file_log: bool = True,
) -> Envelope:
    current_payload, _ = get_bot_process_snapshot(settings, bot_id=bot_id)
    if current_payload["status"] in {"starting", "running_managed"}:
        raise RuntimeError("A UI-managed bot process is already running")
    if current_payload["status"] == "running_external":
        raise RuntimeError("Bot is already running from an external console; stop it there before starting from the UI")

    start_managed_bot_process(
        settings.project_root,
        requested_by=requested_by,
        requested_from=requested_from,
        dry_run=bool(dry_run),
        file_log=bool(file_log),
    )
    payload, source_status = get_bot_process_snapshot(settings, bot_id=bot_id)
    return build_envelope(payload, source_status=[source_status], degraded=False, empty=False, stale=False)


def stop_bot_process_envelope(
    settings: APISettings,
    *,
    requested_by: str,
    bot_id: str = DEFAULT_BOT_ID,
    force: bool = True,
) -> Envelope:
    current_payload, _ = get_bot_process_snapshot(settings, bot_id=bot_id)
    if current_payload["status"] == "running_external":
        raise RuntimeError("Bot is running from an external console and cannot be stopped from the UI manager")
    if current_payload["status"] == "stopped":
        raise RuntimeError("No UI-managed bot process is currently running")

    stop_managed_bot_process(settings.project_root, force=force)
    payload, source_status = get_bot_process_snapshot(settings, bot_id=bot_id)
    payload["last_stopped_by"] = requested_by
    return build_envelope(payload, source_status=[source_status], degraded=False, empty=payload["status"] == "stopped", stale=False)


def runtime_state_is_expected_to_be_absent(process_payload: dict[str, Any]) -> bool:
    return str(process_payload.get("status") or "") in {"stopped", "starting"}
