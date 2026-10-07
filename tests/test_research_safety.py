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


def test_candidate_cannot_change_replay_assumptions() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "changes": {
                "EVENT_REPLAY_LATENCY_SECONDS": 0,
                "EVENT_REPLAY_SLIPPAGE_BPS": 0,
                "EVENT_REPLAY_ALLOW_ROUTE_PROXY": True,
            },
        }
    )

    assert not result.ok
    assert set(result.forbidden_changes) >= {
        "EVENT_REPLAY_LATENCY_SECONDS",
        "EVENT_REPLAY_SLIPPAGE_BPS",
        "EVENT_REPLAY_ALLOW_ROUTE_PROXY",
    }


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


def test_candidate_with_finite_buy_quota_passes() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "target_lanes": ["pump_early_moonshot_micro_lottery"],
            "changes": {"MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS": "3"},
        }
    )

    assert result.ok
    assert result.errors == []


def test_candidate_with_zero_runtime_quota_fails_and_warns_unlimited() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "target_lanes": ["pump_early_moonshot_micro_lottery"],
            "changes": {"MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS": "0"},
        }
    )

    assert not result.ok
    assert "MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS" in result.forbidden_changes
    assert any(error.startswith("runtime_quota_zero_unlimited:MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS") for error in result.errors)
    assert "quota_zero_means_unlimited:MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS" in result.warnings


def test_candidate_with_zero_non_target_quota_warns_only() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "target_lanes": ["analytics_only"],
            "changes": {"SHADOW_FOLLOWUP_MICRO_MAX_OPEN": "0"},
        }
    )

    assert result.ok
    assert result.errors == []
    assert "quota_zero_means_unlimited:SHADOW_FOLLOWUP_MICRO_MAX_OPEN" in result.warnings


def test_candidate_with_negative_quota_fails() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "target_lanes": ["shadow_followup_micro"],
            "changes": {"SHADOW_FOLLOWUP_MICRO_MAX_OPEN": "-1"},
        }
    )

    assert not result.ok
    assert "quota_cap_negative:SHADOW_FOLLOWUP_MICRO_MAX_OPEN:-1.0" in result.errors
    assert "SHADOW_FOLLOWUP_MICRO_MAX_OPEN" in result.forbidden_changes


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


def test_candidate_that_sets_live_research_promotion_fails() -> None:
    result = validate_candidate_safety(
        {
            "live_allowed": False,
            "changes": {"AUTORESEARCH_AUTO_LIVE_PROMOTE": "true"},
        }
    )

    assert not result.ok
    assert "AUTORESEARCH_AUTO_LIVE_PROMOTE" in result.forbidden_changes
    assert "forbidden_true_flag:AUTORESEARCH_AUTO_LIVE_PROMOTE" in result.errors
