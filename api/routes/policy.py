from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.policy import (
    get_config_effect_audit_envelope,
    get_current_baseline_envelope,
    get_decision_ledger_envelope,
    get_drift_envelope,
    get_funnel_attribution_envelope,
    get_model_registry_envelope,
    get_paper_forward_envelope,
    get_policy_replay_envelope,
    get_policy_safety_envelope,
    get_preflight_envelope,
    get_proposals_envelope,
    get_runner_capture_envelope,
    get_trade_diagnostics_envelope,
)
from api.settings import APISettings


router = APIRouter(prefix="/policy", tags=["policy"])


@router.get("/safety", response_model=Envelope)
def policy_safety(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_policy_safety_envelope(settings)


@router.get("/preflight", response_model=Envelope)
def policy_preflight(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_preflight_envelope(settings)


@router.get("/config-effect-audit", response_model=Envelope)
def policy_config_effect_audit(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_config_effect_audit_envelope(settings)


@router.get("/current-baseline", response_model=Envelope)
def policy_current_baseline(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_current_baseline_envelope(settings)


@router.get("/funnel-attribution", response_model=Envelope)
def policy_funnel_attribution(
    limit: int = Query(50, ge=1, le=250),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_funnel_attribution_envelope(settings, limit=limit)


@router.get("/decision-ledger", response_model=Envelope)
def policy_decision_ledger(
    limit: int = Query(50, ge=1, le=250),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_decision_ledger_envelope(settings, limit=limit)


@router.get("/trade-diagnostics", response_model=Envelope)
def policy_trade_diagnostics(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_trade_diagnostics_envelope(settings)


@router.get("/runner-capture", response_model=Envelope)
def policy_runner_capture(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_runner_capture_envelope(settings)


@router.get("/replay", response_model=Envelope)
def policy_replay(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_policy_replay_envelope(settings)


@router.get("/paper-forward", response_model=Envelope)
def policy_paper_forward(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_paper_forward_envelope(settings)


@router.get("/proposals", response_model=Envelope)
def policy_proposals(
    limit: int = Query(25, ge=1, le=100),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_proposals_envelope(settings, limit=limit)


@router.get("/model-registry", response_model=Envelope)
def policy_model_registry(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_model_registry_envelope(settings)


@router.get("/drift", response_model=Envelope)
def policy_drift(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_drift_envelope(settings)


__all__ = ["router"]
