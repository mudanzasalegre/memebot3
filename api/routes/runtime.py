from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.events import get_runtime_events_envelope
from api.services.runtime import get_runtime_state_envelope, get_runtime_strategy_health_envelope
from api.settings import APISettings


router = APIRouter(tags=["runtime"])


@router.get("/runtime/state", response_model=Envelope)
def runtime_state(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_runtime_state_envelope(settings)


@router.get("/runtime/events", response_model=Envelope)
def runtime_events(
    limit: int = Query(default=50, ge=1, le=200),
    before_ts: str | None = Query(default=None),
    address: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_runtime_events_envelope(
        settings,
        limit=limit,
        before_ts=before_ts,
        address=address,
        event_type=event_type,
    )


@router.get("/runtime/strategy-health", response_model=Envelope)
def runtime_strategy_health(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_runtime_strategy_health_envelope(settings)
