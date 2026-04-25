from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from api.repositories.filesystem import parse_timestamp
from runtime.command_bus import (
    CONTROL_COMMAND_STATUSES,
    ensure_control_commands_schema,
    json_dumps,
    json_loads,
    normalize_bot_id,
    normalize_command_status,
    normalize_idempotency_key,
    normalize_requested_from,
    utc_now,
    validate_command_payload,
)


_DATETIME_COLUMNS = {
    "requested_at",
    "started_at",
    "finished_at",
}
_JSON_COLUMNS = {
    "payload_json",
    "result_json",
}


def _connect(db_path: Path) -> sqlite3.Connection:
    ensure_control_commands_schema(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_row(row: sqlite3.Row) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key in _DATETIME_COLUMNS:
            payload[key] = parse_timestamp(value)
            continue
        if key in _JSON_COLUMNS:
            payload[key] = json_loads(value)
            continue
        payload[key] = value
    return {
        "id": int(payload.get("id") or 0),
        "bot_id": payload.get("bot_id"),
        "command_type": payload.get("command_type"),
        "status": payload.get("status"),
        "requested_by": payload.get("requested_by"),
        "requested_from": payload.get("requested_from"),
        "idempotency_key": payload.get("idempotency_key"),
        "requested_at": payload.get("requested_at"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "payload": payload.get("payload_json") or {},
        "result": payload.get("result_json") or {},
        "error_text": payload.get("error_text"),
    }


def list_control_commands(
    db_path: Path,
    *,
    bot_id: str,
    limit: int = 50,
    before_ts: Any = None,
    status: str | None = None,
    command_type: str | None = None,
) -> list[dict[str, Any]]:
    query = [
        "SELECT * FROM control_commands WHERE bot_id = ?",
    ]
    params: list[Any] = [normalize_bot_id(bot_id)]

    if before_ts is not None:
        parsed = parse_timestamp(before_ts)
        if parsed is None:
            raise ValueError("invalid before_ts")
        query.append("AND requested_at < ?")
        params.append(parsed.isoformat())

    if status:
        query.append("AND status = ?")
        params.append(normalize_command_status(status))

    if command_type:
        normalized_type, _ = validate_command_payload(command_type, {})
        query.append("AND command_type = ?")
        params.append(normalized_type)

    query.append("ORDER BY requested_at DESC, id DESC LIMIT ?")
    params.append(int(limit))

    with _connect(db_path) as conn:
        cursor = conn.execute(" ".join(query), tuple(params))
        rows = cursor.fetchall()
    return [_normalize_row(row) for row in rows]


def get_latest_control_command(db_path: Path, *, bot_id: str) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM control_commands WHERE bot_id = ? ORDER BY requested_at DESC, id DESC LIMIT 1",
            (normalize_bot_id(bot_id),),
        ).fetchone()
    return _normalize_row(row) if row is not None else None


def get_control_command_counts(db_path: Path, *, bot_id: str) -> dict[str, int]:
    counts = {status: 0 for status in CONTROL_COMMAND_STATUSES}
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS row_count FROM control_commands WHERE bot_id = ? GROUP BY status",
            (normalize_bot_id(bot_id),),
        ).fetchall()
    for row in rows:
        status = str(row["status"] or "").strip().lower()
        if status in counts:
            counts[status] = int(row["row_count"] or 0)
    return counts


def insert_control_command(
    db_path: Path,
    *,
    bot_id: str,
    command_type: Any,
    payload: Any,
    requested_by: str,
    requested_from: Any = None,
    idempotency_key: Any = None,
) -> tuple[dict[str, Any], bool]:
    normalized_bot_id = normalize_bot_id(bot_id)
    normalized_type, normalized_payload = validate_command_payload(command_type, payload)
    normalized_requested_from = normalize_requested_from(requested_from)
    normalized_idempotency_key = normalize_idempotency_key(idempotency_key)

    with _connect(db_path) as conn:
        if normalized_idempotency_key:
            existing = conn.execute(
                "SELECT * FROM control_commands WHERE bot_id = ? AND idempotency_key = ? LIMIT 1",
                (normalized_bot_id, normalized_idempotency_key),
            ).fetchone()
            if existing is not None:
                return _normalize_row(existing), False

        now = utc_now().isoformat()
        try:
            cursor = conn.execute(
                """
                INSERT INTO control_commands (
                    bot_id,
                    command_type,
                    payload_json,
                    status,
                    requested_by,
                    requested_from,
                    idempotency_key,
                    requested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_bot_id,
                    normalized_type,
                    json_dumps(normalized_payload),
                    "pending",
                    str(requested_by).strip(),
                    normalized_requested_from,
                    normalized_idempotency_key,
                    now,
                ),
            )
            conn.commit()
            row_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            if not normalized_idempotency_key:
                raise
            existing = conn.execute(
                "SELECT * FROM control_commands WHERE bot_id = ? AND idempotency_key = ? LIMIT 1",
                (normalized_bot_id, normalized_idempotency_key),
            ).fetchone()
            if existing is None:
                raise
            return _normalize_row(existing), False

        row = conn.execute(
            "SELECT * FROM control_commands WHERE id = ? LIMIT 1",
            (row_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("control command insert failed")
    return _normalize_row(row), True
