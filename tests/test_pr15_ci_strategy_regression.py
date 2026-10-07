from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import scripts.strategy_quality_gate as gate


def _payload() -> dict:
    path = gate.ROOT / gate.GOLDEN_0707_FIXTURE_RELATIVE_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def _errors(payload: object) -> list[str]:
    errors: list[str] = []
    gate._validate_golden_0707_payload(payload, errors, source_label="unit")
    return errors


def test_golden_0707_fixture_snapshot_contract() -> None:
    payload = _payload()

    assert _errors(payload) == []
    assert payload["audit_summary"]["net_closed_pnl_usd"] == -81.33
    assert payload["audit_summary"]["profit_factor"] == 0.629
    assert payload["audit_summary"]["exit_reason_pnl_usd"]["LIQUIDITY_CRUSH"] == -138.99
    assert payload["audit_summary"]["exit_reason_pnl_usd"]["NO_PUMP_EXIT"] == -55.26
    assert payload["audit_summary"]["partial_pnl_usd"]["no_partial"] == -215.67
    assert payload["audit_summary"]["partial_pnl_usd"]["partial"] == 134.35


def test_golden_0707_rejects_micro_sizing_regression() -> None:
    payload = copy.deepcopy(_payload())
    payload["sizing_cases"][0]["resolved_amount_sol"] = 0.1

    errors = _errors(payload)

    assert any("micro sizing regression" in error for error in errors)


def test_golden_0707_rejects_report_db_divergence() -> None:
    payload = copy.deepcopy(_payload())
    payload["ledger_reconciliation"]["report_closed_rows"] = 3
    payload["ledger_reconciliation"]["report_total_pnl_usd"] = -80.00

    errors = _errors(payload)

    assert any("ledger_reconciliation.report_closed_rows" in error for error in errors)
    assert any("ledger_reconciliation.report_total_pnl_usd" in error for error in errors)


def test_golden_0707_rejects_threshold_floor_regression() -> None:
    payload = copy.deepcopy(_payload())
    payload["threshold_gate"]["activation_ready"] = True
    payload["threshold_gate"]["mode_recommended"] = "enforce"
    payload["threshold_gate"]["fallback_objective_used"] = True

    errors = _errors(payload)

    assert any("must remain inactive when precision floor is missed" in error for error in errors)
    assert any("must recommend shadow" in error for error in errors)
    assert any("must not fallback" in error for error in errors)


def test_golden_0707_rejects_replay_lookahead_regression() -> None:
    payload = copy.deepcopy(_payload())
    replay_case = payload["replay_cases"][0]
    replay_case["lookahead_used"] = True
    replay_case["feature_max_ts"] = "2026-07-07T10:00:01Z"
    replay_case["feature_columns"].append("exit_reason")

    errors = _errors(payload)

    assert any("lookahead_used must be false" in error for error in errors)
    assert any("feature_max_ts > decision_ts" in error for error in errors)
    assert any("forbidden feature column" in error for error in errors)


def test_golden_0707_rejects_safety_regression() -> None:
    payload = copy.deepcopy(_payload())
    payload["safety"]["DRY_RUN"] = False
    payload["safety"]["LIVE_CANARY_ENABLED"] = True
    payload["safety"]["RPC_URL_PRESENT"] = True

    errors = _errors(payload)

    assert "golden_0707 safety requires DRY_RUN=true" in errors
    assert "golden_0707 safety requires LIVE_CANARY_ENABLED=false" in errors
    assert "golden_0707 safety requires RPC_URL_PRESENT=false" in errors


def test_strategy_quality_gate_requires_fixture_in_repo_root(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "strategy_quality_gate.py").write_text("# sentinel\n", encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=True, DRY_RUN=True))

    errors = gate.checks()

    assert f"golden_0707 fixture missing: {gate.GOLDEN_0707_FIXTURE_RELATIVE_PATH.as_posix()}" in errors
