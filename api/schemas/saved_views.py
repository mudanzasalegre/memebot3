from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SavedViewCreateRequest(BaseModel):
    page_key: str
    view_name: str
    filters: dict[str, Any] = Field(default_factory=dict)
    layout: dict[str, Any] = Field(default_factory=dict)


class SavedViewUpdateRequest(BaseModel):
    view_name: str | None = None
    filters: dict[str, Any] | None = None
    layout: dict[str, Any] | None = None
