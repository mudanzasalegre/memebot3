"""One shared extra quote per minute for paired paper experiments.

Reservations count conservatively even if a provider fails or a gate skips.
Primary trading quotes are not throttled here. Alternation only applies when
the other research component has an actual pending quote request.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any


def read(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, sort_keys=True, allow_nan=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def time(value: Any) -> dt.datetime | None:
    try:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(dt.timezone.utc) if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def claim(root: Path | str, owner: str, *, other_pending: bool = False,
          now: dt.datetime | None = None, request_id: str = "") -> bool:
    if owner not in {"runner_exit", "entry_gate"}:
        return False
    stamp = now or dt.datetime.now(dt.timezone.utc)
    if stamp.tzinfo is None:
        return False
    project = Path(root).resolve()
    path = project / "data/research/paired_forward_budget.json"
    if not path.resolve().is_relative_to(project):
        return False
    clock = read(path)
    if path.exists() and clock is None:
        return False  # Corruption must not reset a request budget.
    clock = clock or {}
    previous = time(clock.get("reserved_at"))
    if clock and previous is None:
        return False
    if previous is not None and (stamp - previous).total_seconds() < 60:
        return False
    if other_pending and clock.get("owner") == owner:
        return False
    write(path, {"version": "shared_paper_forward_quote_budget_v1", "owner": owner, "request_id": request_id,
                 "reserved_at": stamp.isoformat(), "extra_quote_calls_per_minute_max": 1})
    return True
