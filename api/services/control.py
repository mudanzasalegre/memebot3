from __future__ import annotations

from typing import Any

from api.repositories.control_commands import (
    get_control_command_counts,
    get_latest_control_command,
    insert_control_command,
    list_control_commands,
)
from api.schemas.common import Envelope
from api.services.bot_process import get_bot_process_snapshot, runtime_state_is_expected_to_be_absent
from api.services.common import build_envelope, make_source_status
from api.services.runtime import (
    DEFAULT_BOT_ID,
    get_runtime_snapshot,
    get_runtime_source_status,
    runtime_snapshot_age_seconds,
    runtime_snapshot_freshness,
    runtime_snapshot_is_stale,
)
from api.services.sources import sqlite_table_status
from api.settings import APISettings
from runtime.command_bus import ensure_control_commands_schema


def _control_table_status(settings: APISettings):
    ensure_control_commands_schema(settings.db_path)
    return sqlite_table_status(settings, table="control_commands", source_key="sqlite.control_commands")


def get_control_state_envelope(settings: APISettings, *, bot_id: str = DEFAULT_BOT_ID) -> Envelope:
    snapshot = get_runtime_snapshot(settings, bot_id=bot_id)
    runtime_status = get_runtime_source_status(settings, snapshot, bot_id=bot_id)
    process_payload, process_status = get_bot_process_snapshot(settings, snapshot=snapshot, bot_id=bot_id)
    commands_status = _control_table_status(settings)
    counts = get_control_command_counts(settings.db_path, bot_id=bot_id)
    last_command = get_latest_control_command(settings.db_path, bot_id=bot_id)
    freshness = runtime_snapshot_freshness(snapshot)
    runtime_age_s = runtime_snapshot_age_seconds(snapshot)

    payload = {
        "bot_id": bot_id,
        "runtime": {
            "updated_at": (snapshot or {}).get("updated_at"),
            "heartbeat_at": (snapshot or {}).get("heartbeat_at"),
            "process_state": (snapshot or {}).get("process_state"),
            "discovery_paused": (snapshot or {}).get("discovery_paused"),
            "buys_paused": (snapshot or {}).get("buys_paused"),
            "retrain_state": (snapshot or {}).get("retrain_state"),
            "reports_refresh_state": (snapshot or {}).get("reports_refresh_state"),
            "last_error": (snapshot or {}).get("last_error"),
            "staleness": freshness,
            "heartbeat_age_s": int(runtime_age_s or 0) if runtime_age_s is not None else None,
        },
        "process": process_payload,
        "commands": {
            "counts_by_status": counts,
            "pending_count": int(counts.get("pending") or 0),
            "running_count": int(counts.get("running") or 0),
            "last_command": last_command,
        },
    }

    statuses = [runtime_status, process_status, commands_status]
    empty = snapshot is None and last_command is None and not any(counts.values())
    runtime_absent_is_expected = runtime_state_is_expected_to_be_absent(process_payload)
    runtime_problem = freshness in {"degraded", "error"} and not runtime_absent_is_expected
    runtime_missing_error = runtime_status.status in {"missing", "error"} and not runtime_absent_is_expected
    degraded = runtime_problem or runtime_missing_error or any(
        item.status in {"missing", "error"} for item in (process_status, commands_status)
    )
    stale = (runtime_snapshot_is_stale(snapshot) and not runtime_absent_is_expected) or any(
        item.status == "stale" for item in (process_status, commands_status)
    )
    return build_envelope(payload, source_status=statuses, empty=empty, degraded=degraded, stale=stale)


def get_control_commands_envelope(
    settings: APISettings,
    *,
    bot_id: str = DEFAULT_BOT_ID,
    limit: int = 50,
    before_ts: str | None = None,
    status: str | None = None,
    command_type: str | None = None,
) -> Envelope:
    items = list_control_commands(
        settings.db_path,
        bot_id=bot_id,
        limit=limit,
        before_ts=before_ts,
        status=status,
        command_type=command_type,
    )
    table_status = _control_table_status(settings)
    data = {
        "items": items,
        "limit": int(limit),
        "before_ts": before_ts,
        "status": status,
        "command_type": command_type,
    }
    return build_envelope(
        data,
        source_status=[table_status],
        empty=not bool(items),
        degraded=table_status.status in {"missing", "error"},
        stale=table_status.status == "stale",
    )


def create_control_command_envelope(
    settings: APISettings,
    *,
    bot_id: str,
    command_type: Any,
    payload: Any,
    requested_by: str,
    requested_from: Any = "ui",
    idempotency_key: Any = None,
) -> Envelope:
    row, inserted = insert_control_command(
        settings.db_path,
        bot_id=bot_id,
        command_type=command_type,
        payload=payload,
        requested_by=requested_by,
        requested_from=requested_from,
        idempotency_key=idempotency_key,
    )
    status = make_source_status(
        source_key="sqlite.control_commands",
        kind="sqlite",
        status="ok",
        detail="inserted" if inserted else "idempotent_replay",
        path=settings.db_path,
    )
    data = {
        "id": row["id"],
        "status": row["status"],
    }
    return build_envelope(data, source_status=[status], empty=False, degraded=False, stale=False)
