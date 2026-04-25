from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


SourceStatusValue = Literal["ok", "empty", "stale", "missing", "error"]


class SourceStatus(BaseModel):
    source_key: str
    kind: str
    status: SourceStatusValue
    updated_at: str | None = None
    detail: str | None = None
    path: str | None = None


class MetaPayload(BaseModel):
    generated_at: str
    degraded: bool = False
    empty: bool = False
    stale: bool = False
    source_status: list[SourceStatus] = Field(default_factory=list)


class Envelope(BaseModel):
    data: Any
    meta: MetaPayload

