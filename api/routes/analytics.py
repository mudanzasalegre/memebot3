from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.analytics import get_baseline_envelope, get_edge_envelope
from api.settings import APISettings


router = APIRouter(tags=["analytics"])


@router.get("/analytics/baseline", response_model=Envelope)
def analytics_baseline(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_baseline_envelope(settings)


@router.get("/analytics/edge", response_model=Envelope)
def analytics_edge(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_edge_envelope(settings)

