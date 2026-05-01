from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = {"proposal_id", "hypothesis", "changes", "expected_effect", "required_gates", "live_allowed", "risk_notes"}


def validate_strategy_proposal(payload: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    missing = sorted(REQUIRED_FIELDS - set(payload))
    if missing:
        errors.append(f"missing_fields:{','.join(missing)}")
    if payload.get("live_allowed") is True:
        gates = set(payload.get("required_gates") or [])
        required = {"policy_replay", "paper_forward", "manual_approval"}
        if not required.issubset(gates):
            errors.append("live_allowed_requires_replay_paper_manual")
    if not isinstance(payload.get("changes", {}), dict):
        errors.append("changes_must_be_object")
    return not errors, errors


def load_and_validate(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    ok, errors = validate_strategy_proposal(payload)
    return {"ok": ok, "errors": errors, "proposal": payload}


__all__ = ["REQUIRED_FIELDS", "load_and_validate", "validate_strategy_proposal"]
