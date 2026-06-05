from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ControlCommandCreateRequest(BaseModel):
    bot_id: str = "main"
    command_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    requested_by: str | None = None
    requested_from: str | None = "ui"
    idempotency_key: str | None = None


class BotProcessStartRequest(BaseModel):
    bot_id: str = "main"
    dry_run: bool = True
    file_log: bool = True
    confirm_live: bool = False
    requested_from: str | None = "ui"


class BotProcessStopRequest(BaseModel):
    bot_id: str = "main"
    force: bool = True
