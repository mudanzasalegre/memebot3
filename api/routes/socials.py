from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.socials import get_social_token_envelope, get_socials_summary_envelope
from api.settings import APISettings


router = APIRouter(tags=["socials"])


@router.get("/socials/{address}", response_model=Envelope)
def token_socials(address: str, settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_social_token_envelope(settings, address=address)


@router.get("/sniper/socials-summary", response_model=Envelope)
def sniper_socials_summary(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_socials_summary_envelope(settings)


__all__ = ["router"]
