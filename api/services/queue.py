from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Any

from api.repositories.filesystem import load_jsonl_rows, parse_timestamp
from api.schemas.common import Envelope, SourceStatus
from api.services.common import build_envelope, iso_or_none, utc_now
from api.services.runtime import (
    DEFAULT_BOT_ID,
    get_runtime_snapshot,
    get_runtime_source_status,
    runtime_snapshot_freshness,
    runtime_snapshot_is_stale,
)
from api.services.sources import jsonl_status
from api.settings import APISettings


def _runtime_snapshot_queue_payload(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    queue_items = (snapshot or {}).get("queue_items_json") or {}
    return {
        "captured_at": queue_items.get("captured_at") or (snapshot or {}).get("updated_at"),
        "pending": (snapshot or {}).get("queue_pending"),
        "requeued": (snapshot or {}).get("queue_requeued"),
        "cooldown": (snapshot or {}).get("queue_cooldown"),
        "oldest_first_seen_at": (snapshot or {}).get("queue_oldest_first_seen_at"),
    }


def _recent_requeue_reasons(settings: APISettings, *, window_min: int = 60) -> list[dict[str, Any]]:
    rows = load_jsonl_rows(settings.runtime_events_path)
    window_start = utc_now() - dt.timedelta(minutes=int(window_min))
    counts: Counter[str] = Counter()
    for row in rows:
        if str(row.get("event_type") or "") != "requeue":
            continue
        ts = parse_timestamp(row.get("ts_utc"))
        if ts is None or ts < window_start:
            continue
        counts[str(row.get("reason") or "unknown")] += 1
    return [
        {"reason": reason, "events": int(events)}
        for reason, events in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def get_queue_summary_envelope(settings: APISettings, *, bot_id: str = DEFAULT_BOT_ID) -> Envelope:
    snapshot = get_runtime_snapshot(settings, bot_id=bot_id)
    runtime_status = get_runtime_source_status(settings, snapshot, bot_id=bot_id)
    events_status = jsonl_status(source_key="metrics.runtime_events", path=settings.runtime_events_path)
    freshness = runtime_snapshot_freshness(snapshot)
    data = {
        **_runtime_snapshot_queue_payload(snapshot),
        "recent_requeue_reasons": _recent_requeue_reasons(settings),
    }
    statuses = [runtime_status, events_status]
    empty = snapshot is None and not data["recent_requeue_reasons"]
    degraded = freshness in {"degraded", "error"} or any(item.status in {"missing", "error"} for item in statuses)
    stale = runtime_snapshot_is_stale(snapshot) or any(item.status == "stale" for item in statuses)
    return build_envelope(
        data,
        source_status=statuses,
        empty=empty,
        degraded=degraded,
        stale=stale,
    )


def _filtered_queue_items(
    snapshot: dict[str, Any] | None,
    *,
    status: str | None,
    limit: int,
    address: str | None,
) -> tuple[str | None, list[dict[str, Any]]]:
    queue_items = (snapshot or {}).get("queue_items_json") or {}
    captured_at = queue_items.get("captured_at") or (snapshot or {}).get("updated_at")
    raw_items = queue_items.get("items") if isinstance(queue_items, dict) else []
    items = raw_items if isinstance(raw_items, list) else []

    filtered: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_status = str(item.get("status") or "")
        item_address = str(item.get("address") or "")
        if status and item_status != status:
            continue
        if address and item_address != address:
            continue
        filtered.append(item)

    filtered.sort(
        key=lambda item: (
            parse_timestamp(item.get("first_seen_at")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            str(item.get("address") or ""),
        )
    )
    return captured_at, filtered[:limit]


def get_queue_items_envelope(
    settings: APISettings,
    *,
    bot_id: str = DEFAULT_BOT_ID,
    status: str | None = None,
    limit: int = 50,
    address: str | None = None,
) -> Envelope:
    snapshot = get_runtime_snapshot(settings, bot_id=bot_id)
    runtime_status = get_runtime_source_status(settings, snapshot, bot_id=bot_id)
    freshness = runtime_snapshot_freshness(snapshot)
    captured_at, items = _filtered_queue_items(snapshot, status=status, limit=limit, address=address)

    queue_items_payload = (snapshot or {}).get("queue_items_json")
    queue_items_present = (
        snapshot is not None
        and isinstance(queue_items_payload, dict)
        and "items" in queue_items_payload
    )
    degraded = freshness in {"degraded", "error"} or snapshot is None or not queue_items_present
    stale = runtime_snapshot_is_stale(snapshot) or runtime_status.status == "stale"

    data = {
        "captured_at": captured_at,
        "items": items,
        "count": len(items),
        "filters": {
            "status": status,
            "limit": limit,
            "address": address,
        },
    }
    return build_envelope(
        data,
        source_status=[runtime_status],
        empty=not items,
        degraded=degraded,
        stale=stale,
    )
