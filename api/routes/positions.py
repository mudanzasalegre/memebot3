from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.positions import get_open_positions_envelope
from api.settings import APISettings


router = APIRouter(tags=["positions"])


@router.get("/positions/open", response_model=Envelope)
def open_positions(
    address: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_open_positions_envelope(settings, address=address, limit=limit)
