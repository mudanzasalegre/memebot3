from __future__ import annotations

import pytest

from research_loop.search_space import get_search_space, load_search_spaces, validate_search_space, validate_search_spaces


def test_loads_minimum_search_spaces() -> None:
    spaces = load_search_spaces()

    for name in [
        "rank_canary",
        "shadow_followup_micro",
        "moonshot_micro",
        "runner_ladder",
        "sniper_momentum",
        "paper_exploration",
    ]:
        assert name in spaces
        assert spaces[name].parameters


def test_search_spaces_validate_safety_contract() -> None:
    result = validate_search_spaces()

    assert result.ok, result.errors


def test_space_keys_do_not_include_live_or_api_protected_keys() -> None:
    spaces = load_search_spaces()

    for space in spaces.values():
        for key in space.parameters:
            upper = key.upper()
            assert "LIVE" not in upper
            assert upper not in {"DISCOVERY_INTERVAL", "SLEEP_SECONDS", "GECKO_RPM", "JUPITER_RPM", "BIRDEYE_RPM"}


def test_alias_resolves_runner_exit_to_runner_ladder() -> None:
    space = get_search_space("runner_exit")

    assert space.name == "runner_ladder"
    assert "BIRD_TP1_PCT" in space.parameters


def test_invalid_space_raises() -> None:
    with pytest.raises(KeyError):
        get_search_space("does_not_exist")


def test_validate_search_space_rejects_live_key() -> None:
    space = get_search_space("moonshot_micro")
    bad = type(space)(
        name="bad",
        parameters={"LIVE_CANARY_ENABLED": [True]},
        target_lanes=["bad"],
        hypothesis="bad",
        expected_effect={"increase_pnl": True},
        risk_notes=["bad"],
    )

    assert not validate_search_space(bad).ok
