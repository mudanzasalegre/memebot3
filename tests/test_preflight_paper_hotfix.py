from __future__ import annotations

import tools.preflight as preflight


def _profile_values() -> dict[str, str]:
    return preflight._load_env_file(preflight.PAPER_HOTFIX_PROFILE)


def test_paper_hotfix_0707_profile_passes_preflight_validation() -> None:
    result = preflight.validate_paper_hotfix_profile()

    assert result["ok"], result["errors"]
    assert result["redacted_values"]["DRY_RUN"] == "1"
    assert result["redacted_values"]["LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED"] == "false"
    assert result["redacted_values"]["PAPER_BOOTSTRAP_ENABLED"] == "true"
    assert result["redacted_values"]["PAPER_BOOTSTRAP_REQUIRE_PUMPSWAP"] == "true"
    assert result["redacted_values"]["PAPER_BOOTSTRAP_AMOUNT_SOL"] == "0.1"
    assert result["redacted_values"]["PAPER_MAX_TRADE_AMOUNT_SOL"] == "0.1"
    assert result["redacted_values"]["SHADOW_FOLLOWUP_MICRO_ENABLED"] == "false"
    assert result["redacted_values"]["ML_RISK_MODEL_ENABLED"] == "false"
    assert result["redacted_values"]["ML_EV_MODEL_ENABLED"] == "false"
    assert result["redacted_values"]["ML_SIZING_ENABLED"] == "false"


def test_paper_hotfix_rejects_zero_micro_cap_as_unlimited() -> None:
    values = _profile_values()
    values["SHADOW_FOLLOWUP_MICRO_MAX_OPEN"] = "0"

    result = preflight.validate_paper_hotfix_values(values)

    assert not result["ok"]
    assert any("SHADOW_FOLLOWUP_MICRO_MAX_OPEN>0 because 0 means unlimited" in error for error in result["errors"])


def test_paper_hotfix_rejects_enabled_live_flag() -> None:
    values = _profile_values()
    values["LIVE_CANARY_ENABLED"] = "true"

    result = preflight.validate_paper_hotfix_values(values)

    assert not result["ok"]
    assert "paper_hotfix_0707 requires LIVE_CANARY_ENABLED=false" in result["errors"]


def test_paper_hotfix_rejects_non_exact_bootstrap_amount() -> None:
    values = _profile_values()
    values["PAPER_BOOTSTRAP_AMOUNT_SOL"] = "0.05"

    result = preflight.validate_paper_hotfix_values(values)

    assert not result["ok"]
    assert "paper_hotfix_0707 requires PAPER_BOOTSTRAP_AMOUNT_SOL=0.1" in result["errors"]


def test_preflight_redacts_secret_env_values() -> None:
    redacted = preflight.redact_env_values(
        {
            "DRY_RUN": "1",
            "HELIUS_API_KEY": "secret-value",
            "WALLET_PRIVATE_KEY": "secret-value",
        }
    )

    assert redacted["DRY_RUN"] == "1"
    assert redacted["HELIUS_API_KEY"] == "<redacted>"
    assert redacted["WALLET_PRIVATE_KEY"] == "<redacted>"


def test_live_canary_templates_pass_preflight_validation() -> None:
    for name in preflight.LIVE_CANARY_TEMPLATE_NAMES:
        result = preflight.validate_live_canary_template_profile(preflight.ROOT / "config" / "profiles" / name)
        assert result["ok"], result["errors"]


def test_live_flags_require_explicit_live_canary_template(tmp_path) -> None:
    profile = tmp_path / "unsafe_live.env"
    profile.write_text(
        "DRY_RUN=0\n"
        "STRATEGY_OPTIMIZATION_LOCK=false\n"
        "LIVE_CANARY_ENABLED=true\n"
        "LIVE_CANARY_MAX_OPEN=1\n"
        "LIVE_CANARY_MAX_DAILY_BUYS=3\n"
        "LIVE_CANARY_DAILY_LOSS_CAP_SOL=0.05\n"
        "LIVE_CANARY_SIZE_SOL=0.01\n",
        encoding="utf-8",
    )

    result = preflight.validate_live_canary_template_profile(profile)

    assert not result["ok"]
    assert any("not an approved live canary template" in error for error in result["errors"])
    assert any("LIVE_CANARY_PROTOCOL=manual_canary_v1" in error for error in result["errors"])


def test_live_canary_template_rejects_zero_caps_as_unlimited(tmp_path) -> None:
    values = preflight._load_env_file(preflight.ROOT / "config" / "profiles" / "live_canary_safe.env")
    values["LIVE_CANARY_MAX_OPEN"] = "0"
    profile = tmp_path / "live_canary_safe.env"
    profile.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")

    result = preflight.validate_live_canary_template_profile(profile)

    assert not result["ok"]
    assert any("LIVE_CANARY_MAX_OPEN must be finite" in error for error in result["errors"])
