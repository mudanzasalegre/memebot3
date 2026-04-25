from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.queue import get_queue_items_envelope, get_queue_summary_envelope
from api.settings import APISettings


router = APIRouter(tags=["queue"])


@router.get("/queue/summary", response_model=Envelope)
def queue_summary(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_queue_summary_envelope(settings)


@router.get("/queue/items", response_model=Envelope)
def queue_items(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    address: str | None = Query(default=None),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_queue_items_envelope(
        settings,
        status=status,
        limit=limit,
        address=address,
    )
