from __future__ import annotations

import datetime as dt
from typing import Any

from api.repositories.filesystem import load_jsonl_rows, parse_timestamp
from api.schemas.common import Envelope
from api.services.common import build_envelope, iso_or_none, to_jsonable
from api.services.sources import jsonl_status
from api.settings import APISettings


def _event_summary(event_type: str, row: dict[str, Any]) -> str:
    if event_type == "queue_add":
        return "added to queue"
    if event_type == "requeue":
        return f"requeue {row.get('reason', 'unknown')} after {row.get('attempts', '?')} attempts"
    if event_type == "queue_drop":
        return f"queue drop: {row.get('reason', 'unknown')}"
    if event_type == "buy":
        return "buy executed"
    if event_type == "ml_decision":
        return f"ml decision passed={row.get('passed')}"
    if event_type == "strategy_decision":
        return f"{row.get('action', 'decision')} via {row.get('effective_mode', 'unknown')}"
    if event_type == "regime_health":
        return f"{row.get('regime', 'unknown')} health {row.get('health_state', 'unknown')}"
    if event_type == "execution":
        return f"{row.get('side', 'trade')} execution ok={row.get('ok')}"
    if event_type == "candidate_stage":
        return f"candidate stage {row.get('stage', 'unknown')}"
    if event_type == "candidate_decision":
        return f"{row.get('decision_action', 'decision')}: {row.get('reason', 'unknown')}"
    if event_type == "candidate_outcome":
        return f"{row.get('source', 'unknown')} outcome {row.get('exit_reason', 'unknown')}"
    if event_type == "candidate_partial":
        return "candidate partial"
    return event_type


def _normalize_event(row: dict[str, Any], index: int) -> dict[str, Any]:
    ts_value = parse_timestamp(row.get("ts_utc"))
    event_type = str(row.get("event_type") or "unknown")
    address = str(row.get("address") or "")
    payload = {
        key: to_jsonable(value)
        for key, value in row.items()
        if key not in {"ts_utc", "event_type", "address"}
    }
    ts_iso = iso_or_none(ts_value) or str(row.get("ts_utc") or "")
    return {
        "id": f"{ts_iso}:{event_type}:{address}:{index}",
        "ts_utc": ts_iso,
        "event_type": event_type,
        "address": address,
        "summary": _event_summary(event_type, row),
        "payload": payload,
    }


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    before_ts: str | None,
    address: str | None,
    event_type: str | None,
) -> list[dict[str, Any]]:
    before_dt = parse_timestamp(before_ts) if before_ts else None
    filtered: list[tuple[dt.datetime | None, dict[str, Any]]] = []
    for row in rows:
        row_event_type = str(row.get("event_type") or "")
        row_address = str(row.get("address") or "")
        if event_type and row_event_type != event_type:
            continue
        if address and row_address != address:
            continue
        row_ts = parse_timestamp(row.get("ts_utc"))
        if before_dt is not None and row_ts is not None and row_ts >= before_dt:
            continue
        filtered.append((row_ts, row))

    filtered.sort(
        key=lambda item: item[0] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    )
    rows_only = [row for _, row in filtered]
    return rows_only[:limit]


def _build_events_envelope(
    *,
    source_key: str,
    source_path,
    rows: list[dict[str, Any]],
    limit: int,
    before_ts: str | None,
    address: str | None,
    event_type: str | None,
) -> Envelope:
    limited = _filter_rows(
        rows,
        limit=limit,
        before_ts=before_ts,
        address=address,
        event_type=event_type,
    )
    items = [_normalize_event(row, index) for index, row in enumerate(limited)]
    statuses = [jsonl_status(source_key=source_key, path=source_path)]
    data = {
        "items": items,
        "count": len(items),
        "filters": {
            "limit": limit,
            "before_ts": before_ts,
            "address": address,
            "event_type": event_type,
        },
    }
    return build_envelope(data, source_status=statuses, empty=not items)


def get_runtime_events_envelope(
    settings: APISettings,
    *,
    limit: int = 50,
    before_ts: str | None = None,
    address: str | None = None,
    event_type: str | None = None,
) -> Envelope:
    rows = load_jsonl_rows(settings.runtime_events_path)
    return _build_events_envelope(
        source_key="metrics.runtime_events",
        source_path=settings.runtime_events_path,
        rows=rows,
        limit=limit,
        before_ts=before_ts,
        address=address,
        event_type=event_type,
    )


def get_research_events_envelope(
    settings: APISettings,
    *,
    limit: int = 50,
    before_ts: str | None = None,
    address: str | None = None,
    event_type: str | None = None,
) -> Envelope:
    rows = load_jsonl_rows(settings.research_events_path)
    return _build_events_envelope(
        source_key="metrics.research_events",
        source_path=settings.research_events_path,
        rows=rows,
        limit=limit,
        before_ts=before_ts,
        address=address,
        event_type=event_type,
    )
