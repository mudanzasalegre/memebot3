from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.discovery import get_discovery_feed_envelope, get_discovery_summary_envelope
from api.settings import APISettings


router = APIRouter(tags=["discovery"])


@router.get("/discovery/feed", response_model=Envelope)
def discovery_feed(
    limit: int = Query(default=50, ge=1, le=200),
    before_ts: str | None = Query(default=None),
    address: str | None = Query(default=None),
    stage: str | None = Query(default=None),
    decision_action: str | None = Query(default=None),
    reason: str | None = Query(default=None),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_discovery_feed_envelope(
        settings,
        limit=limit,
        before_ts=before_ts,
        address=address,
        stage=stage,
        decision_action=decision_action,
        reason=reason,
    )


@router.get("/discovery/summary", response_model=Envelope)
def discovery_summary(
    window_min: int = Query(default=60, ge=1, le=1440),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_discovery_summary_envelope(settings, window_min=window_min)
