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
            "changes": {"MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL": "0.03"},
        }
    )

    assert not result.ok
    assert any(error.startswith("amount_cap_exceeded:MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL") for error in result.errors)


def test_candidate_that_raises_risky_cluster_amount_above_cap_fails() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "changes": {"MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_AMOUNT_SOL": "0.001"},
        }
    )

    assert not result.ok
    assert any(
        error.startswith("amount_cap_exceeded:MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_AMOUNT_SOL")
        for error in result.errors
    )


def test_candidate_that_reintroduces_buy_quota_fails() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "changes": {"MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS": "3"},
        }
    )

    assert not result.ok
    assert "MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS" in result.forbidden_changes
    assert any(error.startswith("unlimited_quota_required:MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS") for error in result.errors)


def test_candidate_that_reintroduces_idle_buy_window_fails() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "changes": {"PAPER_IDLE_AFTER_HOURS": "3"},
        }
    )

    assert not result.ok
    assert "PAPER_IDLE_AFTER_HOURS" in result.forbidden_changes
    assert any(error.startswith("unlimited_quota_required:PAPER_IDLE_AFTER_HOURS") for error in result.errors)


def test_candidate_that_reintroduces_bootstrap_cold_start_fails() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "changes": {"PAPER_BOOTSTRAP_REQUIRE_COLD_START": "true"},
        }
    )

    assert not result.ok
    assert "PAPER_BOOTSTRAP_REQUIRE_COLD_START" in result.forbidden_changes
    assert "required_safe_flag_violation:PAPER_BOOTSTRAP_REQUIRE_COLD_START" in result.errors


def test_candidate_that_reintroduces_global_open_cap_fails() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "changes": {"MAX_ACTIVE_POSITIONS": "12"},
        }
    )

    assert not result.ok
    assert "MAX_ACTIVE_POSITIONS" in result.forbidden_changes
    assert any(error.startswith("unlimited_quota_required:MAX_ACTIVE_POSITIONS") for error in result.errors)
