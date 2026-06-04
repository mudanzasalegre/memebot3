from __future__ import annotations

from research_loop.safety import validate_candidate_safety


def test_safe_candidate_passes() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "changes": {
                "MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL": "75",
                "RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE": "72",
            },
        }
    )

    assert result.ok
    assert result.errors == []


def test_candidate_that_activates_live_fails() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "changes": {"LIVE_CANARY_ENABLED": "true"},
        }
    )

    assert not result.ok
    assert "LIVE_CANARY_ENABLED" in result.forbidden_changes
    assert any("forbidden" in error for error in result.errors)


def test_candidate_that_touches_rpc_url_fails() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "changes": {"RPC_URL": "https://example.invalid"},
        }
    )

    assert not result.ok
    assert "forbidden_env_key:RPC_URL" in result.errors


def test_candidate_that_changes_api_rpm_fails() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "changes": {"GECKO_RPM": "120"},
        }
    )

    assert not result.ok
    assert result.api_budget_risk
    assert "api_budget_protected_key:GECKO_RPM" in result.errors


def test_candidate_that_raises_moonshot_amount_above_cap_fails() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "changes": {"MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL": "0.006"},
        }
    )

    assert not result.ok
    assert any(error.startswith("amount_cap_exceeded:MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL") for error in result.errors)
