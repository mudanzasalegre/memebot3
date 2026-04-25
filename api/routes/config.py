from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.config import get_effective_config_envelope, get_policies_envelope
from api.settings import APISettings


router = APIRouter(tags=["config"])


@router.get("/config/effective", response_model=Envelope)
def config_effective(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_effective_config_envelope(settings)


@router.get("/config/policies", response_model=Envelope)
def config_policies(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_policies_envelope(settings)
