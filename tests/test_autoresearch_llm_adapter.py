from __future__ import annotations

import json

from research_loop.llm_adapter import (
    STATUS_BLOCKED,
    STATUS_DISABLED,
    STATUS_GENERATED,
    STATUS_NO_GENERATOR,
    STATUS_REJECTED,
    load_llm_adapter_config,
    run_llm_adapter,
)
from research_loop.safety import validate_candidate_safety


def _bundle() -> dict:
    return {
        "generated_at_utc": "2026-06-04T00:00:00+00:00",
        "current_run": {},
        "historical": {},
        "api_budget": {"sources": {"mode": "local_files_only"}},
        "recommendation_context": {"source": "local_reports_only"},
    }


def _candidate(proposal_id: str = "ar_llm_001", changes: dict | None = None) -> dict:
    return {
        "proposal_id": proposal_id,
        "created_at_utc": "2026-06-04T00:00:00+00:00",
        "experiment_type": "replay",
        "hypothesis": "LLM adapter generated candidate under schema",
        "target_lanes": ["pump_early_moonshot_micro_lottery"],
        "changes": changes or {"MOONSHOT_MICRO_CONFIRMATION_PNL": "75"},
        "expected_effect": {"increase_pnl": True, "increase_moonshot_capture": True},
        "required_gates": ["replay_positive", "api_budget_ok"],
        "api_budget_sensitive": True,
        "live_allowed": False,
        "risk_notes": ["paper only", "llm generated policy only"],
    }


def test_llm_adapter_defaults_to_disabled_noop(tmp_path) -> None:
    called = False

    def generator(_bundle):
        nonlocal called
        called = True
        return _candidate()

    result = run_llm_adapter(
        _bundle(),
        root=tmp_path,
        output_path=tmp_path / "candidate_policy.json",
        generator=generator,
    )

    assert result.status == STATUS_DISABLED
    assert result.generator_called is False
    assert called is False
    assert not (tmp_path / "candidate_policy.json").exists()


def test_llm_adapter_blocks_unsafe_capability_flags(tmp_path) -> None:
    result = run_llm_adapter(
        _bundle(),
        root=tmp_path,
        env={
            "AUTORESEARCH_LLM_ENABLED": "true",
            "AUTORESEARCH_LLM_CAN_TOUCH_LIVE": "true",
        },
        generator=lambda _bundle: _candidate(),
    )

    assert result.status == STATUS_BLOCKED
    assert result.generator_called is False
    assert "unsafe_llm_capability:AUTORESEARCH_LLM_CAN_TOUCH_LIVE" in result.errors


def test_llm_adapter_enabled_without_generator_is_noop(tmp_path) -> None:
    result = run_llm_adapter(
        _bundle(),
        root=tmp_path,
        env={"AUTORESEARCH_LLM_ENABLED": "true"},
    )

    assert result.status == STATUS_NO_GENERATOR
    assert result.generator_called is False


def test_llm_adapter_validates_and_writes_generated_candidate(tmp_path) -> None:
    output = tmp_path / "candidate_policy.json"

    result = run_llm_adapter(
        _bundle(),
        root=tmp_path,
        output_path=output,
        env={"AUTORESEARCH_LLM_ENABLED": "true"},
        generator=lambda bundle: _candidate("ar_llm_generated") if bundle["recommendation_context"] else None,
    )

    assert result.status == STATUS_GENERATED
    assert result.generator_called is True
    assert output.exists()
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["proposal_id"] == "ar_llm_generated"
    assert saved["live_allowed"] is False


def test_llm_adapter_rejects_invalid_generated_candidate_without_writing(tmp_path) -> None:
    output = tmp_path / "bad_candidate.json"
    result = run_llm_adapter(
        _bundle(),
        root=tmp_path,
        output_path=output,
        env={"AUTORESEARCH_LLM_ENABLED": "true"},
        generator=lambda _bundle: _candidate("ar_llm_bad", {"RPC_URL": "https://example.invalid"}),
    )

    assert result.status == STATUS_REJECTED
    assert result.generator_called is True
    assert not output.exists()
    assert any("RPC_URL" in error for error in result.errors)


def test_llm_adapter_accepts_report_bundle_path(tmp_path) -> None:
    bundle_path = tmp_path / "report_bundle.json"
    bundle_path.write_text(json.dumps(_bundle()), encoding="utf-8")

    result = run_llm_adapter(
        bundle_path,
        root=tmp_path,
        env={"AUTORESEARCH_LLM_ENABLED": "true"},
        generator=lambda _bundle: _candidate("ar_llm_path"),
    )

    assert result.status == STATUS_GENERATED
    assert (tmp_path / "strategy_proposals" / "candidates" / "ar_llm_path.json").exists()


def test_safety_rejects_candidate_enabling_llm_live_or_api_capability() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "changes": {
                "AUTORESEARCH_LLM_ENABLED": "true",
                "AUTORESEARCH_LLM_CAN_TOUCH_LIVE": "true",
                "AUTORESEARCH_LLM_CAN_CALL_APIS": "true",
            },
        }
    )

    assert not result.ok
    assert "AUTORESEARCH_LLM_ENABLED" in result.forbidden_changes
    assert "AUTORESEARCH_LLM_CAN_TOUCH_LIVE" in result.forbidden_changes
    assert "AUTORESEARCH_LLM_CAN_CALL_APIS" in result.forbidden_changes


def test_llm_adapter_config_parses_defaults_and_truthy_values() -> None:
    default = load_llm_adapter_config({})
    enabled = load_llm_adapter_config({"AUTORESEARCH_LLM_ENABLED": "yes"})

    assert default.enabled is False
    assert default.can_edit_code is False
    assert enabled.enabled is True
