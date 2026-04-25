from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select

from db.models import ControlCommand


UTC = dt.timezone.utc
DEFAULT_BOT_ID = "main"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"

CONTROL_COMMAND_STATUSES = (
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_CANCELLED,
)
CONTROL_COMMAND_TYPES = (
    "pause_discovery",
    "resume_discovery",
    "pause_buys",
    "resume_buys",
    "reload_model",
    "trigger_retrain",
    "refresh_reports",
    "set_log_level",
)
CONTROL_REPORT_TYPES = ("baseline", "edge", "research")


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def ensure_control_commands_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS control_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                command_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                requested_from TEXT,
                idempotency_key TEXT,
                requested_at TIMESTAMP NOT NULL,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                result_json TEXT,
                error_text TEXT
            )
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_control_commands_bot_status_requested "
            "ON control_commands (bot_id, status, requested_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_control_commands_bot_command_requested "
            "ON control_commands (bot_id, command_type, requested_at)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_control_commands_bot_idempotency "
            "ON control_commands (bot_id, idempotency_key)"
        )
        conn.commit()


def normalize_command_type(value: Any) -> str:
    command_type = str(value or "").strip().lower()
    if command_type not in CONTROL_COMMAND_TYPES:
        raise ValueError(f"unsupported command_type: {value}")
    return command_type


def normalize_command_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status not in CONTROL_COMMAND_STATUSES:
        raise ValueError(f"unsupported command status: {value}")
    return status


def normalize_requested_by(header_value: Any = None, body_value: Any = None) -> str:
    requested_by = str(header_value or body_value or "").strip()
    if not requested_by:
        raise ValueError("requested_by is required via X-Operator-Id or request body")
    return requested_by


def normalize_bot_id(value: Any) -> str:
    bot_id = str(value or DEFAULT_BOT_ID).strip()
    return bot_id or DEFAULT_BOT_ID


def normalize_idempotency_key(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip()
    return normalized[:160] if normalized else None


def normalize_requested_from(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().lower()
    return normalized[:32] if normalized else None


def _require_object_payload(payload: Any) -> dict[str, Any]:
    if payload in (None, ""):
        return {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    return dict(payload)


def _reject_extra_keys(payload: dict[str, Any], *, allowed: Iterable[str]) -> None:
    allowed_set = {str(key) for key in allowed}
    extras = sorted(key for key in payload if key not in allowed_set)
    if extras:
        raise ValueError(f"unexpected payload keys: {', '.join(extras)}")


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return bool(default)
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def validate_command_payload(command_type: Any, payload: Any) -> tuple[str, dict[str, Any]]:
    normalized_type = normalize_command_type(command_type)
    raw = _require_object_payload(payload)

    if normalized_type in {
        "pause_discovery",
        "resume_discovery",
        "pause_buys",
        "resume_buys",
        "reload_model",
    }:
        _reject_extra_keys(raw, allowed=())
        return normalized_type, {}

    if normalized_type == "trigger_retrain":
        _reject_extra_keys(raw, allowed=("force",))
        return normalized_type, {"force": _to_bool(raw.get("force"), default=False)}

    if normalized_type == "refresh_reports":
        _reject_extra_keys(raw, allowed=("force", "include"))
        include_raw = raw.get("include")
        if include_raw in (None, ""):
            include = list(CONTROL_REPORT_TYPES)
        else:
            if not isinstance(include_raw, list):
                raise ValueError("refresh_reports.include must be an array")
            include = []
            for item in include_raw:
                normalized_item = str(item or "").strip().lower()
                if normalized_item not in CONTROL_REPORT_TYPES:
                    raise ValueError(f"unsupported report include: {item}")
                if normalized_item not in include:
                    include.append(normalized_item)
            if not include:
                raise ValueError("refresh_reports.include cannot be empty")
        return normalized_type, {
            "force": _to_bool(raw.get("force"), default=True),
            "include": include,
        }

    if normalized_type == "set_log_level":
        _reject_extra_keys(raw, allowed=("level", "logger"))
        level_name = str(raw.get("level") or "INFO").strip().upper()
        if level_name not in logging._nameToLevel:
            raise ValueError(f"unsupported log level: {level_name}")
        logger_name = str(raw.get("logger") or "root").strip()
        return normalized_type, {"level": level_name, "logger": logger_name or "root"}

    raise ValueError(f"unsupported command_type: {normalized_type}")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def json_loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw in (None, "", b""):
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _model_to_dict(row: ControlCommand) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "bot_id": row.bot_id,
        "command_type": row.command_type,
        "status": row.status,
        "requested_by": row.requested_by,
        "requested_from": row.requested_from,
        "idempotency_key": row.idempotency_key,
        "requested_at": row.requested_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "payload": json_loads(row.payload_json),
        "result": json_loads(row.result_json),
        "error_text": row.error_text,
    }


async def claim_next_pending_command(session_factory: Any, *, bot_id: str = DEFAULT_BOT_ID) -> dict[str, Any] | None:
    async with session_factory() as session:
        stmt = (
            select(ControlCommand)
            .where(
                ControlCommand.bot_id == str(bot_id),
                ControlCommand.status == STATUS_PENDING,
            )
            .order_by(ControlCommand.requested_at.asc(), ControlCommand.id.asc())
            .limit(1)
        )
        result = await session.execute(stmt)
        row = result.scalars().first()
        if row is None:
            return None

        row.status = STATUS_RUNNING
        row.started_at = utc_now()
        row.finished_at = None
        row.result_json = None
        row.error_text = None
        await session.commit()
        return _model_to_dict(row)


async def complete_command(
    session_factory: Any,
    command_id: int,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error_text: str | None = None,
) -> None:
    normalized_status = normalize_command_status(status)
    async with session_factory() as session:
        row = await session.get(ControlCommand, int(command_id))
        if row is None:
            return
        row.status = normalized_status
        row.finished_at = utc_now()
        row.result_json = json_dumps(result or {}) if result is not None else None
        row.error_text = error_text[:1000] if error_text else None
        await session.commit()
