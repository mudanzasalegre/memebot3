from __future__ import annotations

from fastapi import APIRouter, Query

from api.schemas.common import Envelope
from api.services.common import build_envelope, make_source_status, utc_now
from api.services.sniper_service import hot_queue_status, missed_pumps, sniper_status


router = APIRouter(prefix="/sniper", tags=["sniper"])


def _status(key: str):
    return [make_source_status(source_key=key, kind="derived", status="ok", updated_at=utc_now(), detail="sniper_runtime")]


@router.get("/status", response_model=Envelope)
def get_sniper_status() -> Envelope:
    return build_envelope(sniper_status(), source_status=_status("sniper.status"))


@router.get("/missed-pumps", response_model=Envelope)
def get_missed_pumps(limit: int = Query(50, ge=1, le=250)) -> Envelope:
    return build_envelope(missed_pumps(limit), source_status=_status("sniper.missed_pumps"))


@router.get("/hot-queue", response_model=Envelope)
def get_hot_queue() -> Envelope:
    return build_envelope(hot_queue_status(), source_status=_status("sniper.hot_queue"))


__all__ = ["router"]
