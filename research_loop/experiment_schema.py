from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from research_loop.safety import validate_candidate_safety

REQUIRED_FIELDS = {
    "proposal_id",
    "created_at_utc",
    "experiment_type",
    "hypothesis",
    "target_lanes",
    "changes",
    "expected_effect",
    "required_gates",
    "api_budget_sensitive",
    "live_allowed",
    "risk_notes",
}

ALLOWED_EXPERIMENT_TYPES = {"replay", "paper_forward", "paper_replay", "paper"}


class CandidatePolicyValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CandidatePolicy:
    proposal_id: str
    created_at_utc: str
    experiment_type: str
    hypothesis: str
    target_lanes: list[str]
    changes: dict[str, Any]
    expected_effect: dict[str, Any]
    required_gates: list[str]
    api_budget_sensitive: bool
    live_allowed: bool
    risk_notes: list[str]
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)


def _load_payload(path_or_dict: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(path_or_dict, dict):
        return copy.deepcopy(path_or_dict)
    path = Path(path_or_dict)
    return json.loads(path.read_text(encoding="utf-8"))


def _valid_iso_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item.strip() for item in value)


def _collect_validation_errors(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_FIELDS - set(payload))
    if missing:
        errors.append(f"missing_fields:{','.join(missing)}")

    if not isinstance(payload.get("proposal_id"), str) or not payload.get("proposal_id", "").strip():
        errors.append("proposal_id_required")
    if not _valid_iso_datetime(payload.get("created_at_utc")):
        errors.append("created_at_utc_must_be_iso_datetime")
    if payload.get("experiment_type") not in ALLOWED_EXPERIMENT_TYPES:
        errors.append("experiment_type_invalid")
    if not isinstance(payload.get("hypothesis"), str) or not payload.get("hypothesis", "").strip():
        errors.append("hypothesis_required")
    if not _string_list(payload.get("target_lanes")):
        errors.append("target_lanes_must_be_non_empty_string_list")
    if not isinstance(payload.get("changes"), dict):
        errors.append("changes_must_be_object")
    elif not payload.get("changes"):
        errors.append("changes_must_not_be_empty")
    if not isinstance(payload.get("expected_effect"), dict):
        errors.append("expected_effect_must_be_object")
    if not _string_list(payload.get("required_gates")):
        errors.append("required_gates_must_be_non_empty_string_list")
    if not isinstance(payload.get("api_budget_sensitive"), bool):
        errors.append("api_budget_sensitive_must_be_boolean")
    if payload.get("live_allowed") is not False:
        errors.append("live_allowed_must_be_false")
    if not isinstance(payload.get("risk_notes"), list) or not all(isinstance(item, str) for item in payload.get("risk_notes", [])):
        errors.append("risk_notes_must_be_string_list")

    safety = validate_candidate_safety(payload)
    if not safety.ok:
        errors.extend(f"safety:{error}" for error in safety.errors)
    return errors


def validate_candidate_policy(path_or_dict: str | Path | dict[str, Any]) -> CandidatePolicy:
    payload = _load_payload(path_or_dict)
    if not isinstance(payload, dict):
        raise CandidatePolicyValidationError("candidate_policy_must_be_object")

    errors = _collect_validation_errors(payload)
    if errors:
        raise CandidatePolicyValidationError(";".join(errors))

    return CandidatePolicy(
        proposal_id=payload["proposal_id"],
        created_at_utc=payload["created_at_utc"],
        experiment_type=payload["experiment_type"],
        hypothesis=payload["hypothesis"],
        target_lanes=list(payload["target_lanes"]),
        changes=dict(payload["changes"]),
        expected_effect=dict(payload["expected_effect"]),
        required_gates=list(payload["required_gates"]),
        api_budget_sensitive=payload["api_budget_sensitive"],
        live_allowed=payload["live_allowed"],
        risk_notes=list(payload["risk_notes"]),
        raw=payload,
    )


__all__ = [
    "ALLOWED_EXPERIMENT_TYPES",
    "CandidatePolicy",
    "CandidatePolicyValidationError",
    "REQUIRED_FIELDS",
    "validate_candidate_policy",
]
