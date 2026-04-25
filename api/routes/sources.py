from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.sources import get_sources_status_envelope
from api.settings import APISettings


router = APIRouter(tags=["sources"])


@router.get("/sources/status", response_model=Envelope)
def sources_status(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_sources_status_envelope(settings)

