from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.ml import get_ml_research_envelope, get_ml_status_envelope
from api.settings import APISettings


router = APIRouter(tags=["ml"])


@router.get("/ml/status", response_model=Envelope)
def ml_status(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_ml_status_envelope(settings)


@router.get("/ml/research", response_model=Envelope)
def ml_research(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_ml_research_envelope(settings)

