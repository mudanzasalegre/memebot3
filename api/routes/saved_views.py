from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.auth import UiIdentity, require_authenticated
from api.deps import get_settings
from api.schemas.common import Envelope
from api.schemas.saved_views import SavedViewCreateRequest, SavedViewUpdateRequest
from api.services.saved_views import (
    create_saved_view_envelope,
    delete_saved_view_envelope,
    list_saved_views_envelope,
    update_saved_view_envelope,
)
from api.settings import APISettings


router = APIRouter(tags=["saved-views"])


@router.get("/saved-views", response_model=Envelope)
def list_saved_views(
    page_key: str | None = Query(default=None),
    mine: bool = Query(default=True),
    identity: UiIdentity = Depends(require_authenticated),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return list_saved_views_envelope(
        settings,
        identity=identity,
        page_key=page_key,
        mine=mine,
    )


@router.post("/saved-views", response_model=Envelope)
def create_saved_view(
    payload: SavedViewCreateRequest,
    identity: UiIdentity = Depends(require_authenticated),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return create_saved_view_envelope(
        settings,
        identity=identity,
        page_key=payload.page_key,
        view_name=payload.view_name,
        filters=payload.filters,
        layout=payload.layout,
    )


@router.patch("/saved-views/{view_id}", response_model=Envelope)
def update_saved_view(
    view_id: int,
    payload: SavedViewUpdateRequest,
    identity: UiIdentity = Depends(require_authenticated),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return update_saved_view_envelope(
        settings,
        identity=identity,
        view_id=view_id,
        view_name=payload.view_name,
        filters=payload.filters,
        layout=payload.layout,
    )


@router.delete("/saved-views/{view_id}", response_model=Envelope)
def delete_saved_view(
    view_id: int,
    identity: UiIdentity = Depends(require_authenticated),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return delete_saved_view_envelope(
        settings,
        identity=identity,
        view_id=view_id,
    )
