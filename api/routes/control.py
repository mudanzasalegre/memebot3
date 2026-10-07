from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status

from api.auth import UiIdentity, require_authenticated, require_control_command_permission, require_permission
from api.deps import get_settings
from api.schemas.common import Envelope
from api.schemas.control import BotProcessStartRequest, BotProcessStopRequest, ControlCommandCreateRequest, StackStopRequest
from api.services.control import (
    create_control_command_envelope,
    get_control_commands_envelope,
    get_control_state_envelope,
)
from api.services.live_promotion import build_live_promotion_preflight
from api.services.common import build_envelope
from api.services.runtime import DEFAULT_BOT_ID, get_runtime_snapshot, get_runtime_source_status
from api.services.sources import json_status
from api.services.bot_process import (
    get_bot_process_envelope,
    request_stack_stop_envelope,
    start_bot_process_envelope,
    stop_bot_process_envelope,
)
from api.services.risk_control import get_risk_control_envelope
from api.settings import APISettings


router = APIRouter(tags=["control"])


def _process_http_exception(exc: RuntimeError) -> HTTPException:
    message = str(exc)
    lowered = message.lower()
    if any(token in lowered for token in ("already running", "cannot be stopped", "no ui-managed", "external console")):
        return HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=message)
    return HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail=message)


@router.get("/control/state", response_model=Envelope)
def control_state(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_control_state_envelope(settings)


@router.get("/control/commands", response_model=Envelope)
def control_commands(
    limit: int = Query(default=50, ge=1, le=200),
    before_ts: str | None = Query(default=None),
    status: str | None = Query(default=None),
    command_type: str | None = Query(default=None),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    try:
        return get_control_commands_envelope(
            settings,
            limit=limit,
            before_ts=before_ts,
            status=status,
            command_type=command_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/control/process", response_model=Envelope)
def control_process(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_bot_process_envelope(settings)


@router.get("/control/live-preflight", response_model=Envelope)
def control_live_preflight(settings: APISettings = Depends(get_settings)) -> Envelope:
    runtime_snapshot = get_runtime_snapshot(settings, bot_id=DEFAULT_BOT_ID)
    payload = build_live_promotion_preflight(settings, runtime_snapshot=runtime_snapshot)
    source_status = [
        get_runtime_source_status(settings, runtime_snapshot, bot_id=DEFAULT_BOT_ID),
        json_status(
            source_key="metrics.current_run_summary",
            path=settings.metrics_dir / "current_run_summary.json",
            generated_field="generated_at_utc",
        ),
        json_status(
            source_key="autoresearch.api_budget",
            path=settings.data_dir / "research_runs" / "api_budget.json",
            generated_field="generated_at_utc",
        ),
        json_status(
            source_key="metrics.api_budget_report",
            path=settings.metrics_dir / "api_budget_report.json",
            generated_field="generated_at_utc",
            optional=True,
        ),
        json_status(
            source_key="autoresearch.scoreboard",
            path=settings.data_dir / "research_runs" / "scoreboard.json",
            optional=True,
        ),
    ]
    return build_envelope(payload, source_status=source_status, empty=False, degraded=not bool(payload.get("passed")), stale=False)


@router.get("/control/risk", response_model=Envelope)
def control_risk(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_risk_control_envelope(settings)


@router.post(
    "/control/commands",
    response_model=Envelope,
    status_code=http_status.HTTP_202_ACCEPTED,
)
def create_control_command(
    payload: ControlCommandCreateRequest,
    identity: UiIdentity = Depends(require_authenticated),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    try:
        require_control_command_permission(identity, payload.command_type)
        return create_control_command_envelope(
            settings,
            bot_id=payload.bot_id,
            command_type=payload.command_type,
            payload=payload.payload,
            requested_by=identity.username,
            requested_from=payload.requested_from,
            idempotency_key=payload.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/control/process/start",
    response_model=Envelope,
    status_code=http_status.HTTP_202_ACCEPTED,
)
def start_control_process(
    payload: BotProcessStartRequest,
    identity: UiIdentity = Depends(require_authenticated),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    try:
        require_permission(identity, "control.process.start")
        return start_bot_process_envelope(
            settings,
            requested_by=identity.username,
            requested_from=payload.requested_from or "ui",
            bot_id=payload.bot_id,
            dry_run=payload.dry_run,
            file_log=payload.file_log,
            confirm_live=payload.confirm_live,
            paper_max_invested_sol=payload.paper_max_invested_sol,
            live_max_invested_sol=payload.live_max_invested_sol,
        )
    except RuntimeError as exc:
        raise _process_http_exception(exc) from exc


@router.post(
    "/control/process/stop",
    response_model=Envelope,
    status_code=http_status.HTTP_202_ACCEPTED,
)
def stop_control_process(
    payload: BotProcessStopRequest,
    identity: UiIdentity = Depends(require_authenticated),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    try:
        require_permission(identity, "control.process.stop")
        return stop_bot_process_envelope(
            settings,
            requested_by=identity.username,
            bot_id=payload.bot_id,
            force=payload.force,
        )
    except RuntimeError as exc:
        raise _process_http_exception(exc) from exc


@router.post(
    "/control/stack/stop",
    response_model=Envelope,
    status_code=http_status.HTTP_202_ACCEPTED,
)
def stop_control_stack(
    payload: StackStopRequest,
    identity: UiIdentity = Depends(require_authenticated),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    try:
        require_permission(identity, "control.stack.stop")
        return request_stack_stop_envelope(
            settings,
            requested_by=identity.username,
            bot_id=payload.bot_id,
            force=payload.force,
            delay_seconds=payload.delay_seconds,
        )
    except RuntimeError as exc:
        raise _process_http_exception(exc) from exc
