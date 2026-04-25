from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Response, status as http_status

from api.auth import authenticate_local_user, build_session_token, identity_from_local_user, optional_identity, utc_now
from api.deps import get_settings
from api.schemas.auth import LoginRequest
from api.schemas.common import Envelope
from api.services.auth import get_auth_session_envelope
from api.settings import APISettings


router = APIRouter(tags=["auth"])


def _set_session_cookie(response: Response, settings: APISettings, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=int(settings.session_ttl_seconds),
        httponly=True,
        samesite="lax",
        secure=bool(settings.session_cookie_secure),
        path="/",
    )


def _clear_session_cookie(response: Response, settings: APISettings) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        samesite="lax",
        secure=bool(settings.session_cookie_secure),
        path="/",
    )


@router.get("/auth/session", response_model=Envelope)
def auth_session(
    settings: APISettings = Depends(get_settings),
    identity=Depends(optional_identity),
) -> Envelope:
    return get_auth_session_envelope(settings, identity)


@router.post("/auth/login", response_model=Envelope)
def auth_login(
    payload: LoginRequest,
    response: Response,
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    if settings.auth_mode == "dev":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="login is disabled while UI_AUTH_MODE=dev",
        )

    user = authenticate_local_user(settings, payload.username, payload.password)
    if user is None:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="invalid username or password",
        )

    token = build_session_token(settings, user)
    _set_session_cookie(response, settings, token)
    return get_auth_session_envelope(
        settings,
        identity=identity_from_local_user(
            user,
            settings=settings,
            expires_at=(utc_now() + dt.timedelta(seconds=int(settings.session_ttl_seconds))).isoformat(),
        ),
    )


@router.post("/auth/logout", response_model=Envelope)
def auth_logout(
    response: Response,
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    _clear_session_cookie(response, settings)
    return get_auth_session_envelope(settings, identity=None)
