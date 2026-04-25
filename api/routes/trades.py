from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.trades import (
    get_closed_trades_envelope,
    get_trade_detail_envelope,
    get_trade_replay_envelope,
)
from api.settings import APISettings


router = APIRouter(tags=["trades"])


@router.get("/trades/closed", response_model=Envelope)
def closed_trades(
    limit: int = Query(default=50, ge=1, le=200),
    before_ts: str | None = Query(default=None),
    before_id: int | None = Query(default=None, ge=1),
    outcome: str | None = Query(default=None),
    exit_reason: str | None = Query(default=None),
    entry_regime: str | None = Query(default=None),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_closed_trades_envelope(
        settings,
        limit=limit,
        before_ts=before_ts,
        before_id=before_id,
        outcome=outcome,
        exit_reason=exit_reason,
        entry_regime=entry_regime,
    )


@router.get("/trades/{trade_id}", response_model=Envelope)
def trade_detail(
    trade_id: int,
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_trade_detail_envelope(settings, trade_id=trade_id)


@router.get("/trades/{trade_id}/replay", response_model=Envelope)
def trade_replay(
    trade_id: int,
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_trade_replay_envelope(settings, trade_id=trade_id)
