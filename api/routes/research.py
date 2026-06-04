from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.deps import get_settings
from api.schemas.common import Envelope
from api.services.research import (
    get_research_api_budget_envelope,
    get_research_current_best_envelope,
    get_research_moonshot_progress_envelope,
    get_research_paper_forward_envelope,
    get_research_runs_envelope,
    get_research_scoreboard_envelope,
)
from api.settings import APISettings


router = APIRouter(prefix="/research", tags=["research"])


@router.get("/scoreboard", response_model=Envelope)
def research_scoreboard(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_research_scoreboard_envelope(settings)


@router.get("/runs", response_model=Envelope)
def research_runs(
    limit: int = Query(50, ge=1, le=250),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_research_runs_envelope(settings, limit=limit)


@router.get("/current-best", response_model=Envelope)
def research_current_best(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_research_current_best_envelope(settings)


@router.get("/api-budget", response_model=Envelope)
def research_api_budget(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_research_api_budget_envelope(settings)


@router.get("/moonshot-progress", response_model=Envelope)
def research_moonshot_progress(settings: APISettings = Depends(get_settings)) -> Envelope:
    return get_research_moonshot_progress_envelope(settings)


@router.get("/paper-forward", response_model=Envelope)
def research_paper_forward(
    limit: int = Query(25, ge=1, le=100),
    settings: APISettings = Depends(get_settings),
) -> Envelope:
    return get_research_paper_forward_envelope(settings, limit=limit)


__all__ = ["router"]
