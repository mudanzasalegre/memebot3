from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Any

from api.repositories.filesystem import load_jsonl_rows, parse_timestamp
from api.schemas.common import Envelope, SourceStatus
from api.services.common import build_envelope, iso_or_none, to_jsonable, utc_now
from api.services.sources import jsonl_status
from api.settings import APISettings


_RUNTIME_EVENT_TYPES = {"queue_add", "requeue", "queue_drop", "buy", "ml_decision", "strategy_decision"}
_RESEARCH_EVENT_TYPES = {"candidate_stage", "candidate_decision", "candidate_outcome"}


def _runtime_summary(event_type: str, row: dict[str, Any]) -> str:
    if event_type == "queue_add":
        return "added to queue"
    if event_type == "requeue":
        return f"requeue {row.get('reason', 'unknown')} after {row.get('attempts', '?')} attempts"
    if event_type == "queue_drop":
        return f"queue drop: {row.get('reason', 'unknown')}"
    if event_type == "buy":
        return "buy executed"
    if event_type == "ml_decision":
        passed = bool(row.get("passed"))
        return "ml pass" if passed else "ml block"
    if event_type == "strategy_decision":
        return f"{row.get('action', 'decision')}: {row.get('reason', 'unknown')}"
    return event_type


def _research_summary(event_type: str, row: dict[str, Any]) -> str:
    if event_type == "candidate_stage":
        return f"candidate stage {row.get('stage', 'unknown')}"
    if event_type == "candidate_decision":
        return f"{row.get('decision_action', 'decision')}: {row.get('reason', 'unknown')}"
    if event_type == "candidate_outcome":
        source = row.get("source", "unknown")
        exit_reason = row.get("exit_reason", "unknown")
        pnl_pct = row.get("pnl_pct")
        try:
            pnl_text = f" pnl={float(pnl_pct):.3f}%"
        except Exception:
            pnl_text = ""
        return f"{source} outcome {exit_reason}{pnl_text}"
    return event_type


def _normalize_runtime_event(row: dict[str, Any], index: int) -> dict[str, Any] | None:
    event_type = str(row.get("event_type") or "")
    if event_type not in _RUNTIME_EVENT_TYPES:
        return None

    stage = None
    action = None
    reason = None
    severity = "info"
    regime = row.get("entry_regime") or row.get("regime")

    if event_type == "queue_add":
        stage = "queue"
        action = "added"
    elif event_type == "requeue":
        stage = "queue"
        action = "requeue"
        reason = row.get("reason")
        severity = "warning"
    elif event_type == "queue_drop":
        stage = "queue"
        action = "dropped"
        reason = row.get("reason")
        severity = "warning"
    elif event_type == "buy":
        stage = "execution"
        action = "bought"
        reason = "buy_ok"
        severity = "success"
    elif event_type == "ml_decision":
        stage = "ml"
        passed = bool(row.get("passed"))
        action = "passed" if passed else "blocked"
        reason = "ml_pass" if passed else "ml_block"
        severity = "info" if passed else "warning"
    elif event_type == "strategy_decision":
        stage = "strategy"
        action = str(row.get("action") or "decision")
        reason = row.get("reason")
        if action in {"reject", "rejected", "drop"}:
            severity = "warning"
        elif action in {"buy", "bought"}:
            severity = "success"

    ts_value = parse_timestamp(row.get("ts_utc"))
    ts_iso = iso_or_none(ts_value) or str(row.get("ts_utc") or "")
    payload = {
        key: to_jsonable(value)
        for key, value in row.items()
        if key not in {"ts_utc", "event_type", "address"}
    }
    return {
        "id": f"{ts_iso}:{event_type}:{row.get('address') or ''}:{index}",
        "stream": "runtime",
        "event_type": event_type,
        "ts_utc": ts_iso,
        "address": str(row.get("address") or ""),
        "symbol": row.get("symbol"),
        "regime": regime,
        "stage": stage,
        "action": action,
        "reason": reason,
        "severity": severity,
        "summary": _runtime_summary(event_type, row),
        "payload": payload,
    }


def _normalize_research_event(row: dict[str, Any], index: int) -> dict[str, Any] | None:
    event_type = str(row.get("event_type") or "")
    if event_type not in _RESEARCH_EVENT_TYPES:
        return None

    stage = row.get("stage")
    action = None
    reason = None
    severity = "info"

    if event_type == "candidate_stage":
        action = "observed"
    elif event_type == "candidate_decision":
        action = row.get("decision_action")
        reason = row.get("reason")
        if action == "bought":
            severity = "success"
        elif action == "rejected":
            severity = "warning"
    elif event_type == "candidate_outcome":
        action = "closed"
        reason = row.get("reason") or row.get("exit_reason")
        pnl_pct = row.get("pnl_pct")
        try:
            severity = "success" if pnl_pct is not None and float(pnl_pct) > 0.0 else "warning"
        except Exception:
            severity = "info"

    ts_value = parse_timestamp(row.get("ts_utc"))
    ts_iso = iso_or_none(ts_value) or str(row.get("ts_utc") or "")
    payload = {
        key: to_jsonable(value)
        for key, value in row.items()
        if key not in {"ts_utc", "event_type", "address"}
    }
    return {
        "id": f"{ts_iso}:{event_type}:{row.get('address') or ''}:{index}",
        "stream": "research",
        "event_type": event_type,
        "ts_utc": ts_iso,
        "address": str(row.get("address") or ""),
        "symbol": row.get("symbol"),
        "regime": row.get("regime"),
        "stage": stage,
        "action": action,
        "reason": reason,
        "severity": severity,
        "summary": _research_summary(event_type, row),
        "payload": payload,
    }


def _filter_feed_items(
    items: list[dict[str, Any]],
    *,
    limit: int,
    before_ts: str | None,
    address: str | None,
    stage: str | None,
    decision_action: str | None,
    reason: str | None,
) -> list[dict[str, Any]]:
    before_dt = parse_timestamp(before_ts) if before_ts else None
    filtered: list[tuple[dt.datetime | None, dict[str, Any]]] = []

    for item in items:
        item_address = str(item.get("address") or "")
        item_stage = item.get("stage")
        item_action = item.get("action")
        item_reason = item.get("reason")
        item_ts = parse_timestamp(item.get("ts_utc"))

        if address and item_address != address:
            continue
        if stage and str(item_stage or "") != stage:
            continue
        if decision_action and str(item_action or "") != decision_action:
            continue
        if reason and str(item_reason or "") != reason:
            continue
        if before_dt is not None and item_ts is not None and item_ts >= before_dt:
            continue
        filtered.append((item_ts, item))

    filtered.sort(
        key=lambda pair: pair[0] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    )
    return [row for _, row in filtered[:limit]]


def get_discovery_feed_envelope(
    settings: APISettings,
    *,
    limit: int = 50,
    before_ts: str | None = None,
    address: str | None = None,
    stage: str | None = None,
    decision_action: str | None = None,
    reason: str | None = None,
) -> Envelope:
    runtime_rows = load_jsonl_rows(settings.runtime_events_path)
    research_rows = load_jsonl_rows(settings.research_events_path)

    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(runtime_rows):
        item = _normalize_runtime_event(row, index)
        if item is not None:
            normalized.append(item)
    for index, row in enumerate(research_rows):
        item = _normalize_research_event(row, index)
        if item is not None:
            normalized.append(item)

    items = _filter_feed_items(
        normalized,
        limit=limit,
        before_ts=before_ts,
        address=address,
        stage=stage,
        decision_action=decision_action,
        reason=reason,
    )
    statuses = [
        jsonl_status(source_key="metrics.runtime_events", path=settings.runtime_events_path),
        jsonl_status(source_key="metrics.research_events", path=settings.research_events_path),
    ]
    data = {
        "items": items,
        "count": len(items),
        "filters": {
            "limit": limit,
            "before_ts": before_ts,
            "address": address,
            "stage": stage,
            "decision_action": decision_action,
            "reason": reason,
        },
    }
    return build_envelope(
        data,
        source_status=statuses,
        empty=not items,
        degraded=any(item.status in {"missing", "error"} for item in statuses),
        stale=any(item.status == "stale" for item in statuses),
    )


def _within_window(ts_value: Any, *, window_start: dt.datetime) -> bool:
    ts = parse_timestamp(ts_value)
    return ts is not None and ts >= window_start


def get_discovery_summary_envelope(
    settings: APISettings,
    *,
    window_min: int = 60,
) -> Envelope:
    runtime_rows = load_jsonl_rows(settings.runtime_events_path)
    research_rows = load_jsonl_rows(settings.research_events_path)
    window_start = utc_now() - dt.timedelta(minutes=int(window_min))

    queue_counter = Counter()
    requeue_reasons = Counter()
    for row in runtime_rows:
        if not _within_window(row.get("ts_utc"), window_start=window_start):
            continue
        event_type = str(row.get("event_type") or "")
        if event_type == "queue_add":
            queue_counter["added"] += 1
        elif event_type == "requeue":
            queue_counter["requeued"] += 1
            requeue_reasons[str(row.get("reason") or "unknown")] += 1
        elif event_type == "queue_drop":
            queue_counter["dropped"] += 1
        elif event_type == "buy":
            queue_counter["bought"] += 1

    candidate_decisions = Counter()
    candidate_stages = Counter()
    for row in research_rows:
        if not _within_window(row.get("ts_utc"), window_start=window_start):
            continue
        event_type = str(row.get("event_type") or "")
        if event_type == "candidate_decision":
            action = str(row.get("decision_action") or "unknown")
            raw_reason = str(row.get("reason") or "unknown")
            candidate_decisions[f"{action}:{raw_reason}"] += 1
        elif event_type == "candidate_stage":
            candidate_stages[str(row.get("stage") or "unknown")] += 1

    statuses = [
        jsonl_status(source_key="metrics.runtime_events", path=settings.runtime_events_path),
        jsonl_status(source_key="metrics.research_events", path=settings.research_events_path),
    ]
    data = {
        "window_min": int(window_min),
        "queue": {
            "added": int(queue_counter.get("added", 0)),
            "requeued": int(queue_counter.get("requeued", 0)),
            "dropped": int(queue_counter.get("dropped", 0)),
            "bought": int(queue_counter.get("bought", 0)),
        },
        "candidate_decisions": [
            {"group": group, "count": int(count)}
            for group, count in sorted(candidate_decisions.items(), key=lambda item: (-item[1], item[0]))
        ],
        "candidate_stages": [
            {"group": group, "count": int(count)}
            for group, count in sorted(candidate_stages.items(), key=lambda item: (-item[1], item[0]))
        ],
        "requeue_reasons": [
            {"reason": group, "events": int(count)}
            for group, count in sorted(requeue_reasons.items(), key=lambda item: (-item[1], item[0]))
        ],
    }
    empty = not any(
        (
            data["queue"]["added"],
            data["queue"]["requeued"],
            data["queue"]["dropped"],
            data["queue"]["bought"],
            data["candidate_decisions"],
            data["candidate_stages"],
            data["requeue_reasons"],
        )
    )
    return build_envelope(
        data,
        source_status=statuses,
        empty=empty,
        degraded=any(item.status in {"missing", "error"} for item in statuses),
        stale=any(item.status == "stale" for item in statuses),
    )
