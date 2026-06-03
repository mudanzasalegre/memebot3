from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Iterable

from analytics.report_utils import load_runtime_events
from config.config import PROJECT_ROOT


TIME_FIELDS = (
    "run_started_at",
    "ts_utc",
    "timestamp",
    "created_at",
    "opened_at",
    "closed_at",
    "first_seen_at",
    "updated_at_utc",
)


def parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        out = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        raw = raw.replace("Z", "+00:00")
        try:
            out = dt.datetime.fromisoformat(raw)
        except Exception:
            return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=dt.timezone.utc)
    return out.astimezone(dt.timezone.utc)


def row_time(row: dict[str, Any], *, prefer_run_started: bool = False) -> dt.datetime | None:
    fields = TIME_FIELDS if prefer_run_started else tuple(field for field in TIME_FIELDS if field != "run_started_at")
    for field in fields:
        parsed = parse_time(row.get(field))
        if parsed is not None:
            return parsed
    return None


def current_run_identity(root: Path | None = None, rows: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    runtime_rows = list(rows) if rows is not None else load_runtime_events(root)
    best: dict[str, Any] | None = None
    best_time: dt.datetime | None = None
    for row in runtime_rows:
        started = parse_time(row.get("run_started_at"))
        seen = started or row_time(row)
        run_id = str(row.get("run_id") or "").strip()
        if seen is None and not run_id:
            continue
        key_time = seen or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        if best_time is None or key_time > best_time:
            best_time = key_time
            best = {
                "run_id": run_id or "legacy",
                "run_started_at": started.isoformat() if started else None,
                "selected_at": key_time.isoformat(),
                "source": "runtime_events",
            }
    if best is not None:
        return best
    return {
        "run_id": "legacy",
        "run_started_at": None,
        "selected_at": None,
        "source": "no_runtime_events",
    }


def row_in_current_run(row: dict[str, Any], identity: dict[str, Any]) -> bool:
    run_id = str(identity.get("run_id") or "").strip()
    row_run_id = str(row.get("run_id") or "").strip()
    if run_id and run_id != "legacy":
        if row_run_id:
            return row_run_id == run_id
        started = parse_time(identity.get("run_started_at") or identity.get("selected_at"))
        seen = row_time(row)
        return bool(started is not None and seen is not None and seen >= started)

    started = parse_time(identity.get("run_started_at") or identity.get("selected_at"))
    if started is None:
        return True
    seen = row_time(row)
    return bool(seen is not None and seen >= started)


def filter_current_run_rows(rows: Iterable[dict[str, Any]], identity: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in rows if row_in_current_run(row, identity)]


__all__ = [
    "current_run_identity",
    "filter_current_run_rows",
    "parse_time",
    "row_in_current_run",
    "row_time",
]
