from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.logs import get_logs_tail_envelope
from api.settings import APISettings


router = APIRouter(tags=["logs"])


@router.get("/logs/tail", response_model=Envelope)
def logs_tail(
    target: str = Query(default="app"),
    lines: int = Query(default=200, ge=1, le=1000),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_logs_tail_envelope(settings, target=target, lines=lines)
