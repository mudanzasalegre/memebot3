from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from api.repositories.filesystem import parse_timestamp


_JSON_COLUMNS = {
    "stats_json",
    "ml_gate_json",
    "strategy_health_json",
    "research_json",
    "queue_items_json",
    "build_info_json",
}
_DATETIME_COLUMNS = {
    "updated_at",
    "heartbeat_at",
    "started_at",
    "wallet_checked_at",
    "queue_oldest_first_seen_at",
    "discovery_last_ok_at",
    "monitor_last_ok_at",
    "last_error_at",
}
_BOOL_COLUMNS = {
    "dry_run",
    "discovery_paused",
    "buys_paused",
}


def _safe_json_load(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw in (None, "", b""):
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_row(row: sqlite3.Row) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key in _JSON_COLUMNS:
            payload[key] = _safe_json_load(value)
            continue
        if key in _DATETIME_COLUMNS:
            payload[key] = parse_timestamp(value)
            continue
        if key in _BOOL_COLUMNS and value is not None:
            payload[key] = bool(value)
            continue
        payload[key] = value
    return payload


def load_bot_runtime_state(db_path: Path, *, bot_id: str = "main") -> dict[str, Any] | None:
    if not db_path.exists():
        return None

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bot_runtime_state'"
        )
        if cursor.fetchone() is None:
            return None

        cursor.execute(
            "SELECT * FROM bot_runtime_state WHERE bot_id = ? LIMIT 1",
            (str(bot_id),),
        )
        row = cursor.fetchone()
        if row is None:
            return None
    return _normalize_row(row)
