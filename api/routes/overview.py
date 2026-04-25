from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.overview import get_overview_envelope
from api.settings import APISettings


router = APIRouter(tags=["overview"])


@router.get("/overview", response_model=Envelope)
def overview(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_overview_envelope(settings)
