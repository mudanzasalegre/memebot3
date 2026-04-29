from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from analytics.social_signal import (
    SOCIAL_ENRICHMENT_EVENTS_PATH,
    latest_social_payload,
)
from api.repositories.filesystem import load_jsonl_rows
from api.schemas.common import Envelope, SourceStatus
from api.services.common import build_envelope, make_source_status
from api.services.sources import jsonl_status
from api.settings import APISettings
from runtime.social_enrichment_queue import GLOBAL_SOCIAL_ENRICHMENT_QUEUE


def _social_events_status(path: Path = SOCIAL_ENRICHMENT_EVENTS_PATH) -> SourceStatus:
    return jsonl_status(source_key="metrics.social_enrichment", path=path, optional=True)


def get_social_token_envelope(settings: APISettings, *, address: str) -> Envelope:
    _ = settings
    signal = latest_social_payload(address)
    cached = GLOBAL_SOCIAL_ENRICHMENT_QUEUE.get_cached(address)
    data = {
        "address": address,
        "signal": signal,
        "cached_signal": cached.to_dict() if cached else None,
        "queue": GLOBAL_SOCIAL_ENRICHMENT_QUEUE.snapshot(),
    }
    status = _social_events_status()
    return build_envelope(
        data,
        source_status=[status],
        empty=signal is None and cached is None,
        degraded=status.status == "error",
    )


def get_socials_summary_envelope(settings: APISettings) -> Envelope:
    _ = settings
    rows = load_jsonl_rows(SOCIAL_ENRICHMENT_EVENTS_PATH)
    status_counts: Counter[str] = Counter()
    lane_counts: dict[str, Counter[str]] = defaultdict(Counter)
    risk_flags: Counter[str] = Counter()
    latest_by_address: dict[str, dict[str, Any]] = {}
    for row in rows:
        address = str(row.get("address") or "")
        status = str(row.get("status") or "unknown")
        lane = str(row.get("lane") or "unknown")
        status_counts[status] += 1
        lane_counts[lane][status] += 1
        latest_by_address[address] = row
        for flag in row.get("risk_flags") or []:
            risk_flags[str(flag)] += 1

    present = status_counts.get("present", 0) + status_counts.get("suspicious", 0)
    total = sum(status_counts.values())
    data = {
        "rows": len(rows),
        "unique_tokens": len([key for key in latest_by_address if key]),
        "coverage_pct": (present / total * 100.0) if total else None,
        "status_counts": dict(status_counts),
        "lane_status_counts": {lane: dict(counts) for lane, counts in lane_counts.items()},
        "risk_flags": dict(risk_flags.most_common(20)),
        "queue": GLOBAL_SOCIAL_ENRICHMENT_QUEUE.snapshot(),
    }
    status = _social_events_status()
    return build_envelope(
        data,
        source_status=[status],
        empty=not rows,
        degraded=status.status == "error",
    )


__all__ = ["get_social_token_envelope", "get_socials_summary_envelope"]
