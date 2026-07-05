from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from api.deps import get_settings as api_get_settings
from api.main import create_app
from api.settings import get_settings as load_settings


def test_control_live_preflight_returns_blocked_envelope_on_clean_workspace(tmp_path) -> None:
    data_dir = tmp_path / "data"
    settings = replace(
        load_settings(),
        data_dir=data_dir,
        runtime_dir=data_dir / "runtime",
        metrics_dir=data_dir / "metrics",
        db_path=data_dir / "memebot.sqlite",
        auth_mode="dev",
    )
    app = create_app()
    app.dependency_overrides[api_get_settings] = lambda: settings

    try:
        response = TestClient(app).get("/api/v1/control/live-preflight")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["passed"] is False
    assert payload["data"]["mode"] == "paper_acquisition"
    assert payload["data"]["gates"]
    assert payload["meta"]["degraded"] is True
    assert any(source["source_key"] == "sqlite.bot_runtime_state" for source in payload["meta"]["source_status"])
