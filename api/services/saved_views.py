from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status as http_status

from api.auth import UiIdentity, has_permission, require_permission
from api.repositories.ui_saved_views import (
    create_ui_saved_view,
    delete_ui_saved_view,
    get_ui_saved_view,
    list_ui_saved_views,
    update_ui_saved_view,
)
from api.schemas.common import Envelope
from api.services.common import build_envelope
from api.services.sources import sqlite_table_status
from api.settings import APISettings


def _saved_views_status(settings: APISettings):
    return sqlite_table_status(settings, table="ui_saved_views", source_key="sqlite.ui_saved_views")


def _decorate_item(identity: UiIdentity, item: dict[str, Any]) -> dict[str, Any]:
    created_by = str(item.get("created_by") or "").strip().lower()
    can_edit = created_by == identity.username
    can_delete = can_edit or has_permission(identity, "saved_views.delete_any")
    return {
        **item,
        "can_edit": can_edit,
        "can_delete": can_delete,
    }


def list_saved_views_envelope(
    settings: APISettings,
    *,
    identity: UiIdentity,
    page_key: str | None = None,
    mine: bool = True,
) -> Envelope:
    require_permission(identity, "saved_views.read")
    created_by = identity.username if mine or not has_permission(identity, "saved_views.read_all") else None
    items = [
        _decorate_item(identity, item)
        for item in list_ui_saved_views(
            settings.db_path,
            page_key=page_key,
            created_by=created_by,
        )
    ]
    status = _saved_views_status(settings)
    data = {
        "items": items,
        "page_key": page_key,
        "mine": bool(created_by is not None),
    }
    return build_envelope(
        data,
        source_status=[status],
        empty=not bool(items),
        degraded=status.status in {"missing", "error"},
        stale=False,
    )


def create_saved_view_envelope(
    settings: APISettings,
    *,
    identity: UiIdentity,
    page_key: Any,
    view_name: Any,
    filters: Any,
    layout: Any,
) -> Envelope:
    require_permission(identity, "saved_views.write")
    item = create_ui_saved_view(
        settings.db_path,
        page_key=page_key,
        view_name=view_name,
        filters=filters,
        layout=layout,
        created_by=identity.username,
    )
    status = _saved_views_status(settings)
    return build_envelope(
        _decorate_item(identity, item),
        source_status=[status],
        empty=False,
        degraded=False,
        stale=False,
    )


def update_saved_view_envelope(
    settings: APISettings,
    *,
    identity: UiIdentity,
    view_id: int,
    view_name: Any = None,
    filters: Any = None,
    layout: Any = None,
) -> Envelope:
    require_permission(identity, "saved_views.write")
    current = get_ui_saved_view(settings.db_path, view_id=int(view_id))
    if current is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="saved view not found")
    owner = str(current.get("created_by") or "").strip().lower()
    if owner != identity.username and not has_permission(identity, "saved_views.delete_any"):
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="cannot edit another operator's saved view")
    try:
        item = update_ui_saved_view(
            settings.db_path,
            view_id=int(view_id),
            view_name=view_name,
            filters=filters,
            layout=layout,
        )
    except LookupError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="saved view not found") from exc
    status = _saved_views_status(settings)
    return build_envelope(
        _decorate_item(identity, item),
        source_status=[status],
        empty=False,
        degraded=False,
        stale=False,
    )


def delete_saved_view_envelope(
    settings: APISettings,
    *,
    identity: UiIdentity,
    view_id: int,
) -> Envelope:
    current = get_ui_saved_view(settings.db_path, view_id=int(view_id))
    if current is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="saved view not found")
    owner = str(current.get("created_by") or "").strip().lower()
    if owner != identity.username:
        require_permission(identity, "saved_views.delete_any")
    deleted = delete_ui_saved_view(settings.db_path, view_id=int(view_id))
    if not deleted:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="saved view not found")
    status = _saved_views_status(settings)
    return build_envelope(
        {"id": int(view_id), "deleted": True},
        source_status=[status],
        empty=False,
        degraded=False,
        stale=False,
    )
