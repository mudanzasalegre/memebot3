from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from api.auth import require_authenticated
from api.routes.analytics import router as analytics_router
from api.routes.auth import router as auth_router
from api.routes.config import router as config_router
from api.routes.control import router as control_router
from api.routes.discovery import router as discovery_router
from api.routes.events import router as events_router
from api.routes.health import router as health_router
from api.routes.logs import router as logs_router
from api.routes.ml import router as ml_router
from api.routes.overview import router as overview_router
from api.routes.queue import router as queue_router
from api.routes.policy import router as policy_router
from api.routes.research import router as research_router
from api.routes.runtime import router as runtime_router
from api.routes.positions import router as positions_router
from api.routes.provider_health import router as provider_health_router
from api.routes.saved_views import router as saved_views_router
from api.routes.sniper import router as sniper_router
from api.routes.socials import router as socials_router
from api.routes.sources import router as sources_router
from api.routes.trades import router as trades_router
from api.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.title,
        version=settings.version,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    app.include_router(health_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    protected = [Depends(require_authenticated)]
    app.include_router(sources_router, prefix="/api/v1", dependencies=protected)
    app.include_router(overview_router, prefix="/api/v1", dependencies=protected)
    app.include_router(runtime_router, prefix="/api/v1", dependencies=protected)
    app.include_router(discovery_router, prefix="/api/v1", dependencies=protected)
    app.include_router(queue_router, prefix="/api/v1", dependencies=protected)
    app.include_router(positions_router, prefix="/api/v1", dependencies=protected)
    app.include_router(provider_health_router, prefix="/api/v1", dependencies=protected)
    app.include_router(trades_router, prefix="/api/v1", dependencies=protected)
    app.include_router(analytics_router, prefix="/api/v1", dependencies=protected)
    app.include_router(config_router, prefix="/api/v1", dependencies=protected)
    app.include_router(policy_router, prefix="/api/v1", dependencies=protected)
    app.include_router(research_router, prefix="/api/v1", dependencies=protected)
    app.include_router(research_router, prefix="/api", dependencies=protected)
    app.include_router(control_router, prefix="/api/v1", dependencies=protected)
    app.include_router(saved_views_router, prefix="/api/v1", dependencies=protected)
    app.include_router(ml_router, prefix="/api/v1", dependencies=protected)
    app.include_router(logs_router, prefix="/api/v1", dependencies=protected)
    app.include_router(events_router, prefix="/api/v1", dependencies=protected)
    app.include_router(sniper_router, prefix="/api/v1", dependencies=protected)
    app.include_router(socials_router, prefix="/api/v1", dependencies=protected)
    return app


app = create_app()
