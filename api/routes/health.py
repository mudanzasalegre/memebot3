from __future__ import annotations

from fastapi import APIRouter

from api.schemas.common import Envelope
from api.services.common import build_envelope, make_source_status


router = APIRouter(tags=["health"])


@router.get("/health", response_model=Envelope)
def health() -> Envelope:
    data = {
        "service": "memebot3-api",
        "status": "ok",
        "version": "0.1.0",
    }
    return build_envelope(
        data,
        source_status=[
            make_source_status(
                source_key="api.process",
                kind="service",
                status="ok",
                detail="running",
            )
        ],
    )

