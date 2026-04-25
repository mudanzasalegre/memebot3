from __future__ import annotations

from api.auth import UiIdentity, available_local_users, serialize_identity
from api.schemas.common import Envelope
from api.services.common import build_envelope, make_source_status
from api.settings import APISettings


def get_auth_session_envelope(
    settings: APISettings,
    identity: UiIdentity | None,
) -> Envelope:
    status = make_source_status(
        source_key="api.auth",
        kind="config",
        status="ok",
        detail=f"mode={settings.auth_mode}",
    )
    data = {
        "auth_mode": settings.auth_mode,
        "is_authenticated": identity is not None,
        "user": serialize_identity(identity),
        "available_users": available_local_users(settings),
        "default_credentials_active": settings.using_default_local_auth_users,
        "dev_mode": settings.auth_mode == "dev",
        "loopback_only": settings.auth_mode == "dev",
    }
    return build_envelope(data, source_status=[status], empty=False, degraded=False, stale=False)
