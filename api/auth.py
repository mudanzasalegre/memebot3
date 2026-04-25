from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request, status as http_status

from api.deps import get_settings
from api.settings import APISettings, LocalAuthUserConfig


UTC = dt.timezone.utc
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "testclient"}

VIEWER_PERMISSIONS = {
    "control.read",
    "saved_views.read",
    "saved_views.write",
    "saved_views.delete_own",
}
OPERATOR_PERMISSIONS = VIEWER_PERMISSIONS | {
    "control.command.pause_discovery",
    "control.command.resume_discovery",
    "control.command.pause_buys",
    "control.command.resume_buys",
    "control.command.reload_model",
    "control.command.trigger_retrain",
    "control.command.refresh_reports",
}
ADMIN_PERMISSIONS = OPERATOR_PERMISSIONS | {
    "control.process.start",
    "control.process.stop",
    "control.command.set_log_level",
    "saved_views.read_all",
    "saved_views.delete_any",
}

ROLE_PERMISSIONS = {
    "viewer": VIEWER_PERMISSIONS,
    "operator": OPERATOR_PERMISSIONS,
    "admin": ADMIN_PERMISSIONS,
}

CONTROL_COMMAND_PERMISSIONS = {
    "pause_discovery": "control.command.pause_discovery",
    "resume_discovery": "control.command.resume_discovery",
    "pause_buys": "control.command.pause_buys",
    "resume_buys": "control.command.resume_buys",
    "reload_model": "control.command.reload_model",
    "trigger_retrain": "control.command.trigger_retrain",
    "refresh_reports": "control.command.refresh_reports",
    "set_log_level": "control.command.set_log_level",
}


@dataclass(frozen=True)
class UiIdentity:
    username: str
    display_name: str
    role: str
    permissions: tuple[str, ...]
    auth_mode: str
    is_dev_mode: bool
    expires_at: str | None


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def is_loopback_host(host: str | None) -> bool:
    normalized = str(host or "").strip().lower()
    return normalized in LOOPBACK_HOSTS


def role_permissions(role: str) -> tuple[str, ...]:
    return tuple(sorted(ROLE_PERMISSIONS.get(str(role or "").strip().lower(), VIEWER_PERMISSIONS)))


def serialize_identity(identity: UiIdentity | None) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {
        "username": identity.username,
        "display_name": identity.display_name,
        "role": identity.role,
        "permissions": list(identity.permissions),
        "auth_mode": identity.auth_mode,
        "is_dev_mode": identity.is_dev_mode,
        "expires_at": identity.expires_at,
    }


def available_local_users(settings: APISettings) -> list[dict[str, str]]:
    return [
        {
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
        }
        for user in settings.local_auth_users
    ]


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_decode(raw: str) -> bytes | None:
    try:
        padding = "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(f"{raw}{padding}".encode("ascii"))
    except Exception:
        return None


def _sign_payload(payload_raw: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload_raw, hashlib.sha256).digest()


def _session_payload_to_identity(payload: dict[str, Any], *, auth_mode: str) -> UiIdentity | None:
    username = str(payload.get("sub") or "").strip().lower()
    role = str(payload.get("role") or "").strip().lower()
    display_name = str(payload.get("display_name") or username).strip() or username
    expires_at = payload.get("exp")
    permissions = role_permissions(role)
    if not username or not permissions:
        return None
    return UiIdentity(
        username=username,
        display_name=display_name,
        role=role,
        permissions=permissions,
        auth_mode=auth_mode,
        is_dev_mode=auth_mode == "dev",
        expires_at=str(expires_at) if expires_at else None,
    )


def identity_from_local_user(
    user: LocalAuthUserConfig,
    *,
    settings: APISettings,
    expires_at: str | None,
) -> UiIdentity:
    return UiIdentity(
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        permissions=role_permissions(user.role),
        auth_mode=settings.auth_mode,
        is_dev_mode=settings.auth_mode == "dev",
        expires_at=expires_at,
    )


def build_session_token(settings: APISettings, user: LocalAuthUserConfig) -> str:
    issued_at = utc_now()
    expires_at = issued_at + dt.timedelta(seconds=int(settings.session_ttl_seconds))
    payload = {
        "sub": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "iat": issued_at.isoformat(),
        "exp": expires_at.isoformat(),
    }
    payload_raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = _sign_payload(payload_raw, settings.session_secret)
    return f"{_b64_encode(payload_raw)}.{_b64_encode(signature)}"


def parse_session_token(settings: APISettings, token: str | None) -> UiIdentity | None:
    if token is None or "." not in str(token):
        return None
    payload_part, signature_part = str(token).split(".", 1)
    payload_raw = _b64_decode(payload_part)
    signature_raw = _b64_decode(signature_part)
    if payload_raw is None or signature_raw is None:
        return None
    expected = _sign_payload(payload_raw, settings.session_secret)
    if not hmac.compare_digest(expected, signature_raw):
        return None
    try:
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    expires_at = payload.get("exp")
    if not isinstance(expires_at, str):
        return None
    try:
        expires_dt = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    expires_dt = expires_dt if expires_dt.tzinfo is not None else expires_dt.replace(tzinfo=UTC)
    if expires_dt <= utc_now():
        return None
    return _session_payload_to_identity(payload, auth_mode="local")


def authenticate_local_user(settings: APISettings, username: str, password: str) -> LocalAuthUserConfig | None:
    normalized_username = str(username or "").strip().lower()
    raw_password = str(password or "")
    for user in settings.local_auth_users:
        if not hmac.compare_digest(user.username, normalized_username):
            continue
        if hmac.compare_digest(user.password, raw_password):
            return user
    return None


def resolve_dev_identity(request: Request, settings: APISettings) -> UiIdentity | None:
    if settings.auth_mode != "dev":
        return None
    if not is_loopback_host(request.client.host if request.client else None):
        return None
    return UiIdentity(
        username="dev-local-admin",
        display_name="Local Dev Admin",
        role="admin",
        permissions=role_permissions("admin"),
        auth_mode="dev",
        is_dev_mode=True,
        expires_at=None,
    )


def resolve_identity(request: Request, settings: APISettings) -> UiIdentity | None:
    dev_identity = resolve_dev_identity(request, settings)
    if dev_identity is not None:
        return dev_identity
    return parse_session_token(settings, request.cookies.get(settings.session_cookie_name))


def optional_identity(
    request: Request,
    settings: APISettings = Depends(get_settings),
) -> UiIdentity | None:
    return resolve_identity(request, settings)


def require_authenticated(
    request: Request,
    settings: APISettings = Depends(get_settings),
) -> UiIdentity:
    identity = resolve_identity(request, settings)
    if identity is not None:
        return identity
    if settings.auth_mode == "dev" and not is_loopback_host(request.client.host if request.client else None):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="UI dev auth mode is restricted to loopback clients",
        )
    raise HTTPException(
        status_code=http_status.HTTP_401_UNAUTHORIZED,
        detail="UI session required",
    )


def has_permission(identity: UiIdentity, permission: str) -> bool:
    return permission in identity.permissions


def require_permission(identity: UiIdentity, permission: str) -> None:
    if has_permission(identity, permission):
        return
    raise HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail=f"permission denied: {permission}",
    )


def require_control_command_permission(identity: UiIdentity, command_type: str) -> None:
    permission = CONTROL_COMMAND_PERMISSIONS.get(str(command_type or "").strip().lower())
    if permission is None:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported command_type: {command_type}",
        )
    require_permission(identity, permission)
