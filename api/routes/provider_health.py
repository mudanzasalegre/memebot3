from __future__ import annotations

from fastapi import APIRouter

from api.schemas.common import Envelope
from api.services.common import build_envelope, make_source_status, utc_now
from runtime.provider_health import provider_health_snapshot


router = APIRouter(prefix="/provider-health", tags=["provider-health"])


@router.get("", response_model=Envelope)
def get_provider_health() -> Envelope:
    return build_envelope(
        provider_health_snapshot(),
        source_status=[
            make_source_status(
                source_key="provider_health",
                kind="derived",
                status="ok",
                updated_at=utc_now(),
                detail="logs_and_metrics",
            )
        ],
    )


__all__ = ["router"]
