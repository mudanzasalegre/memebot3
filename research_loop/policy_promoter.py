from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_loop.evaluator import STATUS_ACCEPTED_REPLAY, STATUS_NEEDS_PAPER, EvaluationResult
from research_loop.experiment_schema import CandidatePolicy, validate_candidate_policy
from research_loop.paths import project_root
from research_loop.safety import load_safety_config, validate_candidate_safety
from research_loop.sandbox import SAFE_BASE_ENV, SECRET_MARKERS

PAPER_PROFILE_PREFIX = "paper_research_candidate_"
DEFAULT_SOURCE_PROFILE = "paper_hotfix_runner_v2"
PROFILE_STATUS = "paper_candidate"

FORCED_SAFE_PROFILE_VALUES = {
    **SAFE_BASE_ENV,
    "PAPER_SNIPER_MODE": "true",
    "AUTORESEARCH_PAPER_CANDIDATE": "true",
    "AUTORESEARCH_LIVE_PROMOTION_ENABLED": "false",
    "AUTORESEARCH_AUTO_LIVE_PROMOTE": "false",
    "LIVE_AGGRESSIVE_TRADING_ENABLED": "false",
    "BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED": "false",
    "RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED": "false",
    "POST_PARTIAL_PROTECTION_LIVE_ENABLED": "false",
    "ML_ALLOW_RESEARCH_LIVE": "false",
    "ML_ALLOW_UNKNOWN_LIVE": "false",
    "ALLOW_LIVE_POLICY_ENFORCE": "false",
    "PUMPSWAP_PRIME_STRICT_BUY_ENABLED": "false",
}


class PolicyPromotionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PromotionResult:
    proposal_id: str
    status: str
    profile_name: str
    profile_path: Path
    source_profile: str
    backup_path: Path | None
    safety_report: dict[str, Any]
    promotion_report_path: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "status": self.status,
            "profile_name": self.profile_name,
            "profile_path": str(self.profile_path),
            "source_profile": self.source_profile,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "safety_report": self.safety_report,
            "promotion_report_path": str(self.promotion_report_path) if self.promotion_report_path else None,
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-").lower()
    return cleaned or "candidate"


def _profiles_dir(root: Path) -> Path:
    return root / "config" / "profiles"


def _profile_filename(profile_name: str) -> str:
    name = str(profile_name).strip()
    if not name:
        raise PolicyPromotionError("profile_name_required")
    if any(separator in name for separator in ("/", "\\")):
        raise PolicyPromotionError("profile_name_must_not_be_path")
    if name.endswith(".env"):
        return name
    return f"{name}.env"


def _paper_profile_name(profile_id: str) -> str:
    safe_id = _safe_id(profile_id)
    name = f"{PAPER_PROFILE_PREFIX}{safe_id}"
    if "live" in name.lower():
        raise PolicyPromotionError("paper_profile_id_must_not_contain_live")
    return name


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in SECRET_MARKERS) or "WALLET" in upper


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip().upper()] = value.strip()
    return values


def _env_quote(value: Any) -> str:
    text = str(value)
    if not text or any(char.isspace() for char in text) or any(char in text for char in ['"', "'", "#"]):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _write_env(path: Path, values: dict[str, str]) -> None:
    lines = [
        "# AutoResearch paper candidate profile.",
        "# Generated for paper-forward validation only. No secrets are copied.",
    ]
    for key in sorted(values):
        lines.append(f"{key}={_env_quote(values[key])}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _status_from_evaluation(evaluation_result: EvaluationResult | dict[str, Any] | str | None) -> str:
    if evaluation_result is None:
        return STATUS_ACCEPTED_REPLAY
    if isinstance(evaluation_result, EvaluationResult):
        return evaluation_result.status
    if isinstance(evaluation_result, dict):
        return str(evaluation_result.get("status") or "")
    return str(evaluation_result)


def _validate_promotion_status(
    evaluation_result: EvaluationResult | dict[str, Any] | str | None,
    *,
    allow_needs_paper: bool,
) -> str:
    status = _status_from_evaluation(evaluation_result)
    allowed = {STATUS_ACCEPTED_REPLAY}
    if allow_needs_paper:
        allowed.add(STATUS_NEEDS_PAPER)
    if status not in allowed:
        raise PolicyPromotionError(f"candidate_must_be_accepted_replay:{status or 'missing'}")
    return status


def _filtered_source_env(source_values: dict[str, str]) -> dict[str, str]:
    config = load_safety_config()
    forbidden_env_keys = {str(key).upper() for key in config.get("forbidden_env_keys", [])}
    filtered: dict[str, str] = {}
    for key, value in source_values.items():
        upper = str(key).strip().upper()
        if not upper or upper in forbidden_env_keys or _is_secret_key(upper):
            continue
        filtered[upper] = str(value)
    return filtered


def _profile_values(policy: CandidatePolicy, source_values: dict[str, str]) -> dict[str, str]:
    config = load_safety_config()
    forbidden_env_keys = {str(key).upper() for key in config.get("forbidden_env_keys", [])}
    values = _filtered_source_env(source_values)
    for raw_key, raw_value in policy.changes.items():
        key = str(raw_key).strip().upper()
        if not key or key in forbidden_env_keys or _is_secret_key(key):
            continue
        values[key] = str(raw_value)
    values.update(FORCED_SAFE_PROFILE_VALUES)
    values["AUTORESEARCH_PROPOSAL_ID"] = policy.proposal_id
    return values


def _backup_existing_profile(profile_path: Path, backups_dir: Path) -> Path | None:
    if not profile_path.exists():
        return None
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = backups_dir / f"{profile_path.stem}.{stamp}.env"
    backups_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(profile_path, backup_path)
    return backup_path


def promote_to_paper_candidate(
    candidate_policy: str | Path | dict[str, Any],
    *,
    evaluation_result: EvaluationResult | dict[str, Any] | str | None = STATUS_ACCEPTED_REPLAY,
    root: str | Path | None = None,
    profile_id: str | None = None,
    source_profile: str = DEFAULT_SOURCE_PROFILE,
    promotion_report_path: str | Path | None = None,
    allow_needs_paper: bool = False,
) -> PromotionResult:
    resolved_root = project_root(root)
    status = _validate_promotion_status(evaluation_result, allow_needs_paper=allow_needs_paper)
    policy = validate_candidate_policy(candidate_policy)
    safety = validate_candidate_safety(policy.to_dict())
    safety_report = {
        **safety.as_dict(),
        "generated_at_utc": utc_now(),
        "source_evaluation_status": status,
    }
    if not safety.ok:
        raise PolicyPromotionError(";".join(safety.errors))

    source_filename = _profile_filename(source_profile)
    source_name = source_filename.removesuffix(".env")
    if "live" in source_name.lower():
        raise PolicyPromotionError("source_profile_must_not_be_live")

    profiles_dir = _profiles_dir(resolved_root)
    source_path = profiles_dir / source_filename
    source_values = _parse_env_file(source_path) if source_path.exists() else {}
    profile_name = _paper_profile_name(profile_id or policy.proposal_id)
    profile_path = profiles_dir / f"{profile_name}.env"
    backup_path = _backup_existing_profile(profile_path, profiles_dir / "backups")

    env_values = _profile_values(policy, source_values)
    env_values["AUTORESEARCH_SOURCE_PROFILE"] = source_name
    _write_env(profile_path, env_values)

    report_path = Path(promotion_report_path) if promotion_report_path is not None else None
    result = PromotionResult(
        proposal_id=policy.proposal_id,
        status=PROFILE_STATUS,
        profile_name=profile_name,
        profile_path=profile_path,
        source_profile=source_name,
        backup_path=backup_path,
        safety_report=safety_report,
        promotion_report_path=report_path,
    )
    if report_path is not None:
        _write_json(report_path, result.as_dict())
    return result


__all__ = [
    "DEFAULT_SOURCE_PROFILE",
    "FORCED_SAFE_PROFILE_VALUES",
    "PAPER_PROFILE_PREFIX",
    "PROFILE_STATUS",
    "PolicyPromotionError",
    "PromotionResult",
    "promote_to_paper_candidate",
]
