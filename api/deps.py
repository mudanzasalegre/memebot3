from __future__ import annotations

from api.settings import APISettings, get_settings as _get_settings


def get_settings() -> APISettings:
    return _get_settings()

