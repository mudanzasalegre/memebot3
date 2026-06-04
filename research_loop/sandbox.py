from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_loop.experiment_schema import CandidatePolicy, validate_candidate_policy
from research_loop.paths import project_root, research_runs_dir
from research_loop.safety import load_safety_config, validate_candidate_safety

SAFE_BASE_ENV = {
    "DRY_RUN": "1",
    "STRATEGY_OPTIMIZATION_LOCK": "true",
    "LIVE_CANARY_ENABLED": "false",
    "GREEN_SNIPER_LIVE_ENABLED": "false",
    "RESEARCH_RANK_CANARY_LIVE_ENABLED": "false",
    "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED": "false",
    "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED": "false",
    "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED": "false",
    "LATE_MOMENTUM_WATCH_LIVE_ENABLED": "false",
    "AUTO_PROMOTE_LIVE": "false",
    "MODEL_AUTO_PROMOTE": "false",
    "ML_AUTO_PROMOTE_LANES": "false",
    "LLM_TRADING_ENABLED": "false",
    "SOCIALS_HOT_PATH_BLOCKING": "false",
    "GREEN_SNIPER_REQUIRE_SOCIALS": "false",
}

SECRET_MARKERS = (
    "PRIVATE_KEY",
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "RPC_URL",
    "HELIUS",
    "BIRDEYE",
    "RUGCHECK",
)


class CandidateSandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxResult:
    run_id: str
    run_dir: Path
    candidate_policy_path: Path
    candidate_env_path: Path
    safety_report_path: Path
    config_hash: str
    safety_report: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "candidate_policy_path": str(self.candidate_policy_path),
            "candidate_env_path": str(self.candidate_env_path),
            "safety_report_path": str(self.safety_report_path),
            "config_hash": self.config_hash,
            "safety_report": self.safety_report,
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_run_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return cleaned or "autoresearch_run"


def _default_run_id(policy: CandidatePolicy) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    return _safe_run_id(f"{policy.proposal_id}_{stamp}")


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def _env_quote(value: Any) -> str:
    text = str(value)
    if not text or any(char.isspace() for char in text) or any(char in text for char in ['"', "'", "#"]):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _config_hash(env_values: dict[str, str]) -> str:
    payload = json.dumps(env_values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate_env_values(policy: CandidatePolicy) -> dict[str, str]:
    config = load_safety_config()
    forbidden_env_keys = {str(key).upper() for key in config.get("forbidden_env_keys", [])}
    values = dict(SAFE_BASE_ENV)
    for raw_key, raw_value in policy.changes.items():
        key = str(raw_key).strip().upper()
        if not key:
            continue
        if key in forbidden_env_keys or _is_secret_key(key):
            continue
        values[key] = str(raw_value)
    values.update(SAFE_BASE_ENV)
    return values


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_env(path: Path, env_values: dict[str, str]) -> None:
    lines = [
        "# AutoResearch sandbox profile.",
        "# Generated for replay/paper research only. No secrets are copied.",
    ]
    for key in sorted(env_values):
        lines.append(f"{key}={_env_quote(env_values[key])}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def create_candidate_sandbox(
    candidate_policy: str | Path | dict[str, Any],
    *,
    root: str | Path | None = None,
    run_id: str | None = None,
) -> SandboxResult:
    resolved_root = project_root(root)
    policy = validate_candidate_policy(candidate_policy)
    safety = validate_candidate_safety(policy.to_dict())
    safety_report = safety.as_dict()
    safety_report["generated_at_utc"] = utc_now()
    if not safety.ok:
        raise CandidateSandboxError(";".join(safety.errors))

    resolved_run_id = _safe_run_id(run_id or _default_run_id(policy))
    run_dir = research_runs_dir(resolved_root) / "runs" / resolved_run_id
    env_values = _candidate_env_values(policy)
    config_hash = _config_hash(env_values)
    safety_report["config_hash"] = config_hash
    safety_report["run_id"] = resolved_run_id

    candidate_policy_path = run_dir / "candidate_policy.json"
    candidate_env_path = run_dir / "candidate.env"
    safety_report_path = run_dir / "safety_report.json"
    _write_json(candidate_policy_path, policy.to_dict())
    _write_env(candidate_env_path, env_values)
    _write_json(safety_report_path, safety_report)

    return SandboxResult(
        run_id=resolved_run_id,
        run_dir=run_dir,
        candidate_policy_path=candidate_policy_path,
        candidate_env_path=candidate_env_path,
        safety_report_path=safety_report_path,
        config_hash=config_hash,
        safety_report=safety_report,
    )


__all__ = [
    "CandidateSandboxError",
    "SAFE_BASE_ENV",
    "SandboxResult",
    "create_candidate_sandbox",
]
