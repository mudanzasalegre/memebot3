from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from api.auth import CONTROL_COMMAND_PERMISSIONS, role_permissions
from api.deps import get_settings as api_get_settings
from api.main import create_app
from api.repositories.control_commands import list_control_commands
from api.settings import get_settings as load_settings
from runtime.command_bus import validate_command_payload
from runtime.policy_overlay import evaluate_policy_overlay, set_manual_lane_control


LANE = "pump_early_moonshot_micro_lottery"


def _write_json(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _settings(tmp_path):
    data_dir = tmp_path / "data"
    metrics = data_dir / "metrics"
    return replace(
        load_settings(),
        project_root=tmp_path,
        data_dir=data_dir,
        runtime_dir=data_dir / "runtime",
        metrics_dir=metrics,
        logs_dir=tmp_path / "logs",
        db_path=data_dir / "memebot.sqlite",
        features_dir=data_dir / "features",
        runtime_events_path=metrics / "runtime_events.jsonl",
        research_events_path=metrics / "research_events.jsonl",
        research_scorecard_json=metrics / "research_scorecard.json",
        research_thresholds_json=metrics / "research_thresholds.json",
        recommended_threshold_json=metrics / "recommended_threshold.json",
        train_status_json=metrics / "train_status.json",
        dataset_quality_json=metrics / "dataset_quality.json",
        paper_portfolio_path=data_dir / "paper_portfolio.json",
        auth_mode="dev",
    )


def test_risk_control_api_returns_lane_contract_and_reasons(tmp_path) -> None:
    settings = _settings(tmp_path)
    _write_json(
        settings.paper_portfolio_path,
        {
            "positions": [
                {
                    "address": "LOSS1",
                    "closed": True,
                    "closed_at": "2026-07-07T10:00:00+00:00",
                    "entry_lane": LANE,
                    "total_pnl_usd": -138.99,
                    "total_pnl_pct": -31.0,
                    "exit_reason": "LIQUIDITY_CRUSH",
                },
                {
                    "address": "WIN1",
                    "closed": True,
                    "closed_at": "2026-07-07T10:10:00+00:00",
                    "entry_lane": LANE,
                    "total_pnl_usd": 57.66,
                    "total_pnl_pct": 22.0,
                    "exit_reason": "POST_PARTIAL_TRAILING",
                },
            ]
        },
    )
    _write_json(
        settings.metrics_dir / "current_run_summary.json",
        {"generated_at_utc": "2026-07-07T10:20:00+00:00", "total_pnl_usd": -81.33},
    )
    _write_json(
        settings.metrics_dir / "current_run_trade_diagnostics.json",
        {
            "generated_at_utc": "2026-07-07T10:20:00+00:00",
            "by_lane": {
                LANE: {
                    "pnl_rows": 2,
                    "avg_pnl_pct": -4.5,
                    "total_pnl_pct_points": -9.0,
                    "severe_losses": 1,
                    "buys": 1,
                    "shadows": 3,
                    "peak_100": 2,
                    "policy_category": "moonshot_micro_lottery",
                }
            },
        },
    )
    _write_json(
        settings.metrics_dir / "current_run_missed_pumps.json",
        {
            "generated_at_utc": "2026-07-07T10:20:00+00:00",
            "rows": [
                {
                    "address": "MISS1",
                    "lane": LANE,
                    "reason": "cluster_bad",
                    "peak_pct": 350,
                }
            ],
        },
    )
    set_manual_lane_control(LANE, disabled=True, root=tmp_path, reason="test_disable", requested_by="pytest")

    app = create_app()
    app.dependency_overrides[api_get_settings] = lambda: settings
    try:
        response = TestClient(app).get("/api/v1/control/risk")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    data = payload["data"]
    assert data["summary"]["gross_spot_closed_pnl_usd"] == -81.33
    # Kept for older clients, but explicitly documented as a gross-spot alias.
    assert data["summary"]["net_closed_pnl_usd"] == -81.33
    assert data["pnl_accounting"] == {
        "basis": "gross_spot",
        "canonical_field": "gross_spot_closed_pnl_usd",
        "fees_included": False,
        "network_fees_included": False,
        "priority_fees_included": False,
        "legacy_aliases": {"net_closed_pnl_usd": "gross_spot_closed_pnl_usd"},
    }
    assert data["summary"]["profit_factor"] == pytest.approx(57.66 / 138.99)
    assert data["summary"]["severe_loss_count"] == 1
    lane = next(row for row in data["lanes"] if row["lane"] == LANE)
    assert lane["manual_disabled"] is True
    assert lane["disabled_source"] == "manual"
    assert lane["cap_warning"] == "cap_zero"
    assert data["top_loss_reasons"][0]["reason"] == "LIQUIDITY_CRUSH"
    assert data["top_missed_moonshot_reasons"][0] == {"reason": "cluster_bad", "count": 1}


def test_lane_control_payload_validation_and_permission_contract() -> None:
    command_type, payload = validate_command_payload(
        "disable_lane",
        {"lane": "Moonshot Micro Lottery", "reason": "loss lane"},
    )

    assert command_type == "disable_lane"
    assert payload == {"lane": "moonshot_micro_lottery", "reason": "loss lane"}
    assert CONTROL_COMMAND_PERMISSIONS["disable_lane"] == "control.command.disable_lane"
    assert CONTROL_COMMAND_PERMISSIONS["enable_lane"] == "control.command.enable_lane"
    assert "control.command.disable_lane" in role_permissions("operator")


def test_manual_lane_control_blocks_dry_run_without_touching_live(tmp_path) -> None:
    set_manual_lane_control(LANE, disabled=True, root=tmp_path, reason="operator_test", requested_by="pytest")

    paper_decision = evaluate_policy_overlay(
        {"entry_lane": LANE},
        dry_run=True,
        live=False,
        root=tmp_path,
        enabled=False,
    )
    live_decision = evaluate_policy_overlay(
        {"entry_lane": LANE},
        dry_run=False,
        live=True,
        root=tmp_path,
        enabled=True,
    )

    assert paper_decision.allowed is False
    assert "manual_disable_lane" in paper_decision.reason
    assert live_decision.allowed is True
    assert live_decision.reason == "live_guard_no_runtime_overlay"


def test_lane_kill_switch_command_creation_is_idempotent(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = create_app()
    app.dependency_overrides[api_get_settings] = lambda: settings
    request = {
        "bot_id": "main",
        "command_type": "disable_lane",
        "payload": {"lane": LANE, "reason": "operator_test"},
        "requested_from": "ui",
        "idempotency_key": "disable-lane-test-key",
    }
    try:
        client = TestClient(app)
        first = client.post("/api/v1/control/commands", json=request)
        second = client.post("/api/v1/control/commands", json=request)
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["data"] == second.json()["data"]
    rows = list_control_commands(settings.db_path, bot_id="main", limit=10, command_type="disable_lane")
    assert len(rows) == 1
    assert rows[0]["payload"]["lane"] == LANE
