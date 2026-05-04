from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _check_runtime() -> None:
    print(f"python_executable={sys.executable}")
    print(f"python_version={sys.version.split()[0]}")
    if EXPECTED_PYTHON.exists():
        print(f"expected_python={EXPECTED_PYTHON}")
        if Path(sys.executable).resolve() != EXPECTED_PYTHON.resolve():
            raise SystemExit(
                "API smoke must be executed with the project venv. "
                f"Use: {EXPECTED_PYTHON} scripts/api_smoke.py"
            )


def _check_imports() -> None:
    for name in ("fastapi", "uvicorn", "pydantic", "httpx", "starlette"):
        mod = importlib.import_module(name)
        print(f"{name}={getattr(mod, '__version__', 'ok')}")


def _require_mapping(value: object, *, path: str) -> dict:
    if not isinstance(value, dict):
        raise SystemExit(f"Expected mapping payload for {path}")
    return value


def _require_keys(payload: dict, keys: tuple[str, ...], *, path: str) -> None:
    missing = [key for key in keys if key not in payload]
    if missing:
        raise SystemExit(f"Missing keys for {path}: {', '.join(missing)}")


def _require_source_key(payload: dict, source_key: str, *, path: str) -> None:
    source_status = (((payload.get("meta") or {}).get("source_status")) or [])
    if not any(isinstance(item, dict) and item.get("source_key") == source_key for item in source_status):
        raise SystemExit(f"Missing source status {source_key} for {path}")


def _exercise_api() -> None:
    from api.deps import get_settings
    from api.repositories.runtime_state import load_bot_runtime_state
    from api.main import app

    auth_endpoints = [
        "/api/v1/health",
        "/api/v1/auth/session",
    ]
    endpoints = [
        "/api/v1/sources/status",
        "/api/v1/overview",
        "/api/v1/runtime/state",
        "/api/v1/runtime/events?limit=3",
        "/api/v1/runtime/strategy-health",
        "/api/v1/discovery/feed?limit=5",
        "/api/v1/discovery/summary?window_min=60",
        "/api/v1/queue/summary",
        "/api/v1/queue/items?limit=5",
        "/api/v1/positions/open?limit=5",
        "/api/v1/trades/closed?limit=5",
        "/api/v1/analytics/baseline",
        "/api/v1/analytics/edge",
        "/api/v1/config/effective",
        "/api/v1/config/policies",
        "/api/v1/ml/status",
        "/api/v1/ml/research",
        "/api/v1/control/state",
        "/api/v1/control/process",
        "/api/v1/control/commands?limit=5",
        "/api/v1/saved-views?page_key=control",
        "/api/v1/logs/tail?target=app&lines=5",
        "/api/v1/events/runtime?limit=3",
        "/api/v1/events/research?limit=3",
        "/api/v1/sniper/status",
        "/api/v1/sniper/missed-pumps?limit=5",
        "/api/v1/sniper/hot-queue",
        "/api/v1/sniper/socials-summary",
        "/api/v1/provider-health",
        "/api/v1/policy/safety",
        "/api/v1/policy/replay",
        "/api/v1/policy/decision-ledger?limit=5",
        "/api/v1/policy/funnel-attribution?limit=5",
        "/api/v1/policy/trade-diagnostics",
        "/api/v1/policy/runner-capture",
        "/api/v1/policy/proposals?limit=5",
        "/api/v1/policy/model-registry",
    ]

    with TestClient(app) as client:
        auth_payloads: dict[str, dict] = {}
        for path in auth_endpoints:
            response = client.get(path)
            print(f"{path}={response.status_code}")
            if response.status_code != 200:
                raise SystemExit(f"Endpoint failed: {path} -> {response.status_code}")
            payload = response.json()
            if "data" not in payload or "meta" not in payload:
                raise SystemExit(f"Envelope missing keys for {path}")
            auth_payloads[path] = payload

        auth_session = _require_mapping(auth_payloads["/api/v1/auth/session"].get("data"), path="/api/v1/auth/session")
        _require_keys(
            auth_session,
            ("auth_mode", "is_authenticated", "available_users", "default_credentials_active"),
            path="/api/v1/auth/session",
        )

        unauth_response = client.get("/api/v1/control/state")
        print(f"/api/v1/control/state[unauth]={unauth_response.status_code}")
        if auth_session.get("auth_mode") == "local" and unauth_response.status_code != 401:
            raise SystemExit("Expected /api/v1/control/state to require auth in local mode")

        if not auth_session.get("is_authenticated"):
            if auth_session.get("auth_mode") == "dev":
                raise SystemExit("Dev mode session should already be authenticated")
            smoke_username = os.getenv("UI_SMOKE_USERNAME") or ("admin" if auth_session.get("default_credentials_active") else "")
            smoke_password = os.getenv("UI_SMOKE_PASSWORD") or ("admin" if auth_session.get("default_credentials_active") else "")
            if not smoke_username or not smoke_password:
                raise SystemExit("Set UI_SMOKE_USERNAME/UI_SMOKE_PASSWORD or enable default local credentials")
            login_response = client.post(
                "/api/v1/auth/login",
                json={"username": smoke_username, "password": smoke_password},
            )
            print(f"/api/v1/auth/login={login_response.status_code}")
            if login_response.status_code != 200:
                raise SystemExit(f"Endpoint failed: /api/v1/auth/login -> {login_response.status_code}")

        payloads: dict[str, dict] = {}
        for path in endpoints:
            response = client.get(path)
            print(f"{path}={response.status_code}")
            if response.status_code != 200:
                raise SystemExit(f"Endpoint failed: {path} -> {response.status_code}")
            payload = response.json()
            if "data" not in payload or "meta" not in payload:
                raise SystemExit(f"Envelope missing keys for {path}")
            payloads[path] = payload

        baseline = _require_mapping(payloads["/api/v1/analytics/baseline"].get("data"), path="/api/v1/analytics/baseline")
        _require_keys(baseline, ("project_root", "config", "positions", "features"), path="/api/v1/analytics/baseline")

        edge = _require_mapping(payloads["/api/v1/analytics/edge"].get("data"), path="/api/v1/analytics/edge")
        _require_keys(edge, ("project_root", "overview"), path="/api/v1/analytics/edge")

        config_effective = _require_mapping(payloads["/api/v1/config/effective"].get("data"), path="/api/v1/config/effective")
        _require_keys(config_effective, ("DRY_RUN", "AI_THRESHOLD", "ML_GATE_MODE"), path="/api/v1/config/effective")
        _require_source_key(payloads["/api/v1/config/effective"], "config.cfg", path="/api/v1/config/effective")

        config_policies = _require_mapping(payloads["/api/v1/config/policies"].get("data"), path="/api/v1/config/policies")
        _require_keys(config_policies, ("filters", "sizing", "exit", "strategy"), path="/api/v1/config/policies")
        _require_source_key(payloads["/api/v1/config/policies"], "config.policies", path="/api/v1/config/policies")

        ml_status = _require_mapping(payloads["/api/v1/ml/status"].get("data"), path="/api/v1/ml/status")
        _require_keys(
            ml_status,
            ("runtime", "gate", "train_status", "recommended_threshold", "dataset_quality"),
            path="/api/v1/ml/status",
        )
        ml_runtime = _require_mapping(ml_status.get("runtime"), path="/api/v1/ml/status.runtime")
        _require_keys(
            ml_runtime,
            ("model_exists", "meta_exists", "model_loaded", "features_count", "activation_ready", "dataset_quality_passed"),
            path="/api/v1/ml/status.runtime",
        )
        ml_gate = _require_mapping(ml_status.get("gate"), path="/api/v1/ml/status.gate")
        _require_keys(ml_gate, ("mode", "enforced", "threshold"), path="/api/v1/ml/status.gate")

        ml_research = _require_mapping(payloads["/api/v1/ml/research"].get("data"), path="/api/v1/ml/research")
        _require_keys(ml_research, ("scorecard", "thresholds", "research_events"), path="/api/v1/ml/research")

        policy_safety = _require_mapping(payloads["/api/v1/policy/safety"].get("data"), path="/api/v1/policy/safety")
        _require_keys(policy_safety, ("gates", "invariants", "policy_replay", "proposals"), path="/api/v1/policy/safety")
        _require_source_key(payloads["/api/v1/policy/safety"], "policy.safety", path="/api/v1/policy/safety")

        policy_replay = _require_mapping(payloads["/api/v1/policy/replay"].get("data"), path="/api/v1/policy/replay")
        _require_keys(policy_replay, ("current", "best_by_total_pnl", "policies", "raw"), path="/api/v1/policy/replay")

        policy_ledger = _require_mapping(
            payloads["/api/v1/policy/decision-ledger?limit=5"].get("data"),
            path="/api/v1/policy/decision-ledger",
        )
        _require_keys(policy_ledger, ("count", "summary", "items"), path="/api/v1/policy/decision-ledger")

        policy_funnel = _require_mapping(
            payloads["/api/v1/policy/funnel-attribution?limit=5"].get("data"),
            path="/api/v1/policy/funnel-attribution",
        )
        _require_keys(policy_funnel, ("count", "summary", "items"), path="/api/v1/policy/funnel-attribution")

        policy_proposals = _require_mapping(
            payloads["/api/v1/policy/proposals?limit=5"].get("data"),
            path="/api/v1/policy/proposals",
        )
        _require_keys(policy_proposals, ("count", "counts", "items"), path="/api/v1/policy/proposals")

        control_state = _require_mapping(payloads["/api/v1/control/state"].get("data"), path="/api/v1/control/state")
        _require_keys(control_state, ("bot_id", "runtime", "process", "commands"), path="/api/v1/control/state")

        control_process = _require_mapping(payloads["/api/v1/control/process"].get("data"), path="/api/v1/control/process")
        _require_keys(
            control_process,
            ("status", "managed", "external", "can_start", "can_stop", "state_file_path"),
            path="/api/v1/control/process",
        )

        control_history = _require_mapping(payloads["/api/v1/control/commands?limit=5"].get("data"), path="/api/v1/control/commands")
        _require_keys(control_history, ("items", "limit"), path="/api/v1/control/commands")
        _require_source_key(payloads["/api/v1/control/state"], "runtime.bot_process_manager", path="/api/v1/control/state")
        _require_source_key(payloads["/api/v1/control/process"], "runtime.bot_process_manager", path="/api/v1/control/process")
        _require_source_key(payloads["/api/v1/control/state"], "sqlite.control_commands", path="/api/v1/control/state")
        _require_source_key(payloads["/api/v1/control/commands?limit=5"], "sqlite.control_commands", path="/api/v1/control/commands")
        _require_source_key(payloads["/api/v1/saved-views?page_key=control"], "sqlite.ui_saved_views", path="/api/v1/saved-views")

        settings = get_settings()
        runtime_snapshot = load_bot_runtime_state(settings.db_path, bot_id="main")
        runtime_gate = ((runtime_snapshot or {}).get("ml_gate_json") or {}) if runtime_snapshot else {}
        if runtime_gate:
            _require_source_key(payloads["/api/v1/ml/status"], "sqlite.bot_runtime_state", path="/api/v1/ml/status")
            for key in ("mode", "enforced", "threshold", "activation_ready"):
                if runtime_gate.get(key) is not None and ml_gate.get(key) != runtime_gate.get(key):
                    raise SystemExit(f"Runtime gate mismatch for /api/v1/ml/status.gate.{key}")
            for key in ("model_loaded", "features_count", "threshold_metric", "rows", "dataset_quality_passed"):
                if runtime_gate.get(key) is not None and ml_runtime.get(key) != runtime_gate.get(key):
                    raise SystemExit(f"Runtime merge mismatch for /api/v1/ml/status.runtime.{key}")

        post_response = client.post(
            "/api/v1/control/commands",
            json={
                "bot_id": "api-smoke",
                "command_type": "pause_discovery",
                "payload": {},
                "idempotency_key": "api-smoke-pause-discovery-v1",
            },
        )
        print(f"/api/v1/control/commands[POST]={post_response.status_code}")
        if post_response.status_code != 202:
            raise SystemExit(f"Endpoint failed: /api/v1/control/commands [POST] -> {post_response.status_code}")
        post_payload = post_response.json()
        if "data" not in post_payload or "meta" not in post_payload:
            raise SystemExit("Envelope missing keys for /api/v1/control/commands [POST]")
        post_data = _require_mapping(post_payload.get("data"), path="/api/v1/control/commands[POST]")
        _require_keys(post_data, ("id", "status"), path="/api/v1/control/commands[POST]")
        _require_source_key(post_payload, "sqlite.control_commands", path="/api/v1/control/commands[POST]")

        saved_view_create = client.post(
            "/api/v1/saved-views",
            json={
                "page_key": "api-smoke",
                "view_name": "Smoke view",
                "filters": {"statusFilter": "all"},
                "layout": {"variant": "smoke"},
            },
        )
        print(f"/api/v1/saved-views[POST]={saved_view_create.status_code}")
        if saved_view_create.status_code != 200:
            raise SystemExit(f"Endpoint failed: /api/v1/saved-views [POST] -> {saved_view_create.status_code}")
        saved_view_payload = saved_view_create.json()
        saved_view_data = _require_mapping(saved_view_payload.get("data"), path="/api/v1/saved-views[POST]")
        _require_keys(saved_view_data, ("id", "page_key", "view_name", "filters"), path="/api/v1/saved-views[POST]")
        saved_view_id = int(saved_view_data["id"])

        saved_view_patch = client.patch(
            f"/api/v1/saved-views/{saved_view_id}",
            json={
                "view_name": "Smoke view updated",
                "filters": {"statusFilter": "pending"},
            },
        )
        print(f"/api/v1/saved-views/{saved_view_id}[PATCH]={saved_view_patch.status_code}")
        if saved_view_patch.status_code != 200:
            raise SystemExit(f"Endpoint failed: /api/v1/saved-views/{saved_view_id} [PATCH] -> {saved_view_patch.status_code}")

        saved_view_delete = client.delete(f"/api/v1/saved-views/{saved_view_id}")
        print(f"/api/v1/saved-views/{saved_view_id}[DELETE]={saved_view_delete.status_code}")
        if saved_view_delete.status_code != 200:
            raise SystemExit(f"Endpoint failed: /api/v1/saved-views/{saved_view_id} [DELETE] -> {saved_view_delete.status_code}")

        closed_response = client.get("/api/v1/trades/closed?limit=1")
        if closed_response.status_code != 200:
            raise SystemExit(f"Endpoint failed: /api/v1/trades/closed?limit=1 -> {closed_response.status_code}")
        closed_payload = closed_response.json()
        items = ((closed_payload.get("data") or {}).get("items") or [])
        if items:
            trade_id = items[0].get("trade_id")
            for path in (
                f"/api/v1/trades/{trade_id}",
                f"/api/v1/trades/{trade_id}/replay",
            ):
                response = client.get(path)
                print(f"{path}={response.status_code}")
                if response.status_code != 200:
                    raise SystemExit(f"Endpoint failed: {path} -> {response.status_code}")
                payload = response.json()
                if "data" not in payload or "meta" not in payload:
                    raise SystemExit(f"Envelope missing keys for {path}")


def main() -> None:
    _check_runtime()
    _check_imports()
    _exercise_api()
    print("api_smoke=ok")


if __name__ == "__main__":
    main()
