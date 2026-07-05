from __future__ import annotations

import pytest

from api.auth import CONTROL_COMMAND_PERMISSIONS, role_permissions
from runtime.command_bus import validate_command_payload


def test_run_autoresearch_payload_defaults_are_validated() -> None:
    command_type, payload = validate_command_payload("run_autoresearch", {})

    assert command_type == "run_autoresearch"
    assert payload == {
        "force": True,
        "space": None,
        "max_candidates": 25,
        "max_parallel": 1,
        "mode": "seeded_random",
        "regenerate_reports": False,
    }


def test_run_autoresearch_payload_accepts_manual_controls() -> None:
    _, payload = validate_command_payload(
        "run_autoresearch",
        {
            "force": False,
            "space": "moonshot_micro",
            "max_candidates": "8",
            "max_parallel": "2",
            "mode": "bandit_suggested",
            "regenerate_reports": True,
        },
    )

    assert payload["force"] is False
    assert payload["space"] == "moonshot_micro"
    assert payload["max_candidates"] == 8
    assert payload["max_parallel"] == 2
    assert payload["mode"] == "bandit_suggested"
    assert payload["regenerate_reports"] is True


def test_run_autoresearch_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unsupported autoresearch mode"):
        validate_command_payload("run_autoresearch", {"mode": "unknown"})


def test_run_autoresearch_operator_permission_is_registered() -> None:
    permission = CONTROL_COMMAND_PERMISSIONS["run_autoresearch"]

    assert permission == "control.command.run_autoresearch"
    assert permission in role_permissions("operator")
