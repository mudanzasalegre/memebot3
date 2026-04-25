from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from api.repositories.filesystem import parse_timestamp
from runtime.command_bus import json_dumps, json_loads, utc_now


_DATETIME_COLUMNS = {"created_at", "updated_at"}
_JSON_COLUMNS = {"filters_json", "layout_json"}


def ensure_ui_saved_views_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ui_saved_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_key TEXT NOT NULL,
                view_name TEXT NOT NULL,
                filters_json TEXT NOT NULL DEFAULT '{}',
                layout_json TEXT,
                created_by TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_ui_saved_views_page_owner_updated "
            "ON ui_saved_views (page_key, created_by, updated_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_ui_saved_views_owner_updated "
            "ON ui_saved_views (created_by, updated_at)"
        )
        conn.commit()


def _connect(db_path: Path) -> sqlite3.Connection:
    ensure_ui_saved_views_schema(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_page_key(value: Any) -> str:
    page_key = str(value or "").strip().lower()
    if not page_key:
        raise ValueError("page_key is required")
    return page_key[:64]


def normalize_view_name(value: Any) -> str:
    view_name = str(value or "").strip()
    if not view_name:
        raise ValueError("view_name is required")
    return view_name[:128]


def normalize_owner(value: Any) -> str:
    owner = str(value or "").strip().lower()
    if not owner:
        raise ValueError("created_by is required")
    return owner[:128]


def _normalize_object(value: Any, *, field_name: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return dict(value)


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
        "page_key": payload.get("page_key"),
        "view_name": payload.get("view_name"),
        "filters": payload.get("filters_json") or {},
        "layout": payload.get("layout_json") or {},
        "created_by": payload.get("created_by"),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
    }


def list_ui_saved_views(
    db_path: Path,
    *,
    page_key: Any = None,
    created_by: Any = None,
) -> list[dict[str, Any]]:
    query = ["SELECT * FROM ui_saved_views WHERE 1=1"]
    params: list[Any] = []
    if page_key not in (None, ""):
        query.append("AND page_key = ?")
        params.append(normalize_page_key(page_key))
    if created_by not in (None, ""):
        query.append("AND created_by = ?")
        params.append(normalize_owner(created_by))
    query.append("ORDER BY updated_at DESC, id DESC")
    with _connect(db_path) as conn:
        rows = conn.execute(" ".join(query), tuple(params)).fetchall()
    return [_normalize_row(row) for row in rows]


def get_ui_saved_view(db_path: Path, *, view_id: int) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM ui_saved_views WHERE id = ? LIMIT 1",
            (int(view_id),),
        ).fetchone()
    return _normalize_row(row) if row is not None else None


def create_ui_saved_view(
    db_path: Path,
    *,
    page_key: Any,
    view_name: Any,
    filters: Any,
    layout: Any,
    created_by: Any,
) -> dict[str, Any]:
    normalized_page_key = normalize_page_key(page_key)
    normalized_view_name = normalize_view_name(view_name)
    normalized_filters = _normalize_object(filters, field_name="filters")
    normalized_layout = _normalize_object(layout, field_name="layout")
    normalized_owner = normalize_owner(created_by)
    now = utc_now().isoformat()

    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO ui_saved_views (
                page_key,
                view_name,
                filters_json,
                layout_json,
                created_by,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_page_key,
                normalized_view_name,
                json_dumps(normalized_filters),
                json_dumps(normalized_layout),
                normalized_owner,
                now,
                now,
            ),
        )
        conn.commit()
        row_id = int(cursor.lastrowid)
        row = conn.execute("SELECT * FROM ui_saved_views WHERE id = ? LIMIT 1", (row_id,)).fetchone()
    if row is None:
        raise RuntimeError("ui_saved_view insert failed")
    return _normalize_row(row)


def update_ui_saved_view(
    db_path: Path,
    *,
    view_id: int,
    view_name: Any = None,
    filters: Any = None,
    layout: Any = None,
) -> dict[str, Any]:
    updates: list[str] = []
    params: list[Any] = []
    if view_name is not None:
        updates.append("view_name = ?")
        params.append(normalize_view_name(view_name))
    if filters is not None:
        updates.append("filters_json = ?")
        params.append(json_dumps(_normalize_object(filters, field_name="filters")))
    if layout is not None:
        updates.append("layout_json = ?")
        params.append(json_dumps(_normalize_object(layout, field_name="layout")))
    if not updates:
        raise ValueError("at least one field must be provided")
    updates.append("updated_at = ?")
    params.append(utc_now().isoformat())
    params.append(int(view_id))

    with _connect(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE ui_saved_views SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
        conn.commit()
        if int(cursor.rowcount or 0) <= 0:
            raise LookupError("saved view not found")
        row = conn.execute("SELECT * FROM ui_saved_views WHERE id = ? LIMIT 1", (int(view_id),)).fetchone()
    if row is None:
        raise LookupError("saved view not found")
    return _normalize_row(row)


def delete_ui_saved_view(db_path: Path, *, view_id: int) -> bool:
    with _connect(db_path) as conn:
        cursor = conn.execute("DELETE FROM ui_saved_views WHERE id = ?", (int(view_id),))
        conn.commit()
        return int(cursor.rowcount or 0) > 0
