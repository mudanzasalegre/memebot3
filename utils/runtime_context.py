from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any


_CONTEXT: dict[str, Any] = {
    "run_id": os.getenv("MEMEBOT_RUN_ID", "").strip(),
    "started_at": os.getenv("MEMEBOT_RUN_STARTED_AT", "").strip(),
    "test_event": False,
}


def set_runtime_context(
    *,
    run_id: str | int | None = None,
    started_at: datetime | str | None = None,
    test_event: bool | None = None,
) -> dict[str, Any]:
    if run_id is not None:
        _CONTEXT["run_id"] = str(run_id).strip()
        if _CONTEXT["run_id"]:
            os.environ["MEMEBOT_RUN_ID"] = _CONTEXT["run_id"]
    if started_at is not None:
        if isinstance(started_at, datetime):
            ts = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
            _CONTEXT["started_at"] = ts.astimezone(timezone.utc).isoformat()
        else:
            _CONTEXT["started_at"] = str(started_at).strip()
        if _CONTEXT["started_at"]:
            os.environ["MEMEBOT_RUN_STARTED_AT"] = _CONTEXT["started_at"]
    if test_event is not None:
        _CONTEXT["test_event"] = bool(test_event)
    return get_runtime_context()


def get_runtime_context() -> dict[str, Any]:
    return {
        "run_id": str(_CONTEXT.get("run_id") or os.getenv("MEMEBOT_RUN_ID", "")).strip(),
        "started_at": str(_CONTEXT.get("started_at") or os.getenv("MEMEBOT_RUN_STARTED_AT", "")).strip(),
        "test_event": bool(_CONTEXT.get("test_event", False)),
    }


def runtime_context_payload(*, run_id: str | None = None, test_event: bool | None = None) -> dict[str, Any]:
    ctx = get_runtime_context()
    out: dict[str, Any] = {}
    effective_run_id = str(run_id if run_id is not None else ctx.get("run_id") or "").strip()
    if effective_run_id:
        out["run_id"] = effective_run_id
    if ctx.get("started_at"):
        out["run_started_at"] = ctx["started_at"]
    effective_test = bool(ctx.get("test_event")) if test_event is None else bool(test_event)
    if effective_test:
        out["test_event"] = True
        out.setdefault("run_id", "SMOKE")
    return out


__all__ = ["get_runtime_context", "runtime_context_payload", "set_runtime_context"]
