from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_loop.paths import project_root, research_runs_dir

CHECKPOINT_JSON = "checkpoint.json"


@dataclass(frozen=True)
class DuplicateCheck:
    duplicate: bool
    reasons: list[str] = field(default_factory=list)
    matching_runs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "duplicate": self.duplicate,
            "reasons": list(self.reasons),
            "matching_runs": list(self.matching_runs),
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def checkpoint_path(root: str | Path | None = None) -> Path:
    return research_runs_dir(project_root(root)) / CHECKPOINT_JSON


def empty_checkpoint() -> dict[str, Any]:
    return {
        "generated_at_utc": utc_now(),
        "proposal_ids": [],
        "changes_hashes": [],
        "config_hashes": [],
        "runs": [],
    }


def load_checkpoint(root: str | Path | None = None, *, path: str | Path | None = None) -> dict[str, Any]:
    payload = _read_json(Path(path) if path is not None else checkpoint_path(root))
    if not isinstance(payload, dict):
        return empty_checkpoint()
    payload.setdefault("proposal_ids", [])
    payload.setdefault("changes_hashes", [])
    payload.setdefault("config_hashes", [])
    payload.setdefault("runs", [])
    return payload


def save_checkpoint(
    checkpoint: dict[str, Any],
    *,
    root: str | Path | None = None,
    path: str | Path | None = None,
) -> Path:
    checkpoint["generated_at_utc"] = utc_now()
    output = Path(path) if path is not None else checkpoint_path(root)
    _write_json(output, checkpoint)
    return output


def changes_hash(candidate_policy: dict[str, Any]) -> str:
    payload = candidate_policy.get("changes") if isinstance(candidate_policy, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def candidate_duplicate_check(
    candidate_policy: dict[str, Any],
    checkpoint: dict[str, Any],
    *,
    config_hash: str | None = None,
) -> DuplicateCheck:
    proposal_id = str(candidate_policy.get("proposal_id") or "")
    chash = changes_hash(candidate_policy)
    reasons: list[str] = []
    matching_runs: list[str] = []

    if proposal_id and proposal_id in set(checkpoint.get("proposal_ids") or []):
        reasons.append("proposal_id")
    if chash in set(checkpoint.get("changes_hashes") or []):
        reasons.append("changes_hash")
    if config_hash and config_hash in set(checkpoint.get("config_hashes") or []):
        reasons.append("config_hash")

    for run in checkpoint.get("runs") or []:
        if not isinstance(run, dict):
            continue
        if proposal_id and run.get("proposal_id") == proposal_id:
            matching_runs.append(str(run.get("run_id") or ""))
        elif run.get("changes_hash") == chash:
            matching_runs.append(str(run.get("run_id") or ""))
        elif config_hash and run.get("config_hash") == config_hash:
            matching_runs.append(str(run.get("run_id") or ""))

    return DuplicateCheck(
        duplicate=bool(reasons),
        reasons=sorted(set(reasons)),
        matching_runs=sorted(set(item for item in matching_runs if item)),
    )


def record_checkpoint_run(
    checkpoint: dict[str, Any],
    *,
    run_id: str,
    candidate_policy: dict[str, Any],
    status: str,
    config_hash: str | None = None,
    objective_score: float | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    proposal_id = str(candidate_policy.get("proposal_id") or "")
    chash = changes_hash(candidate_policy)
    if proposal_id and proposal_id not in checkpoint["proposal_ids"]:
        checkpoint["proposal_ids"].append(proposal_id)
    if chash not in checkpoint["changes_hashes"]:
        checkpoint["changes_hashes"].append(chash)
    if config_hash and config_hash not in checkpoint["config_hashes"]:
        checkpoint["config_hashes"].append(config_hash)

    entry = {
        "run_id": run_id,
        "proposal_id": proposal_id,
        "status": status,
        "changes_hash": chash,
        "config_hash": config_hash,
        "objective_score": objective_score,
        "error": error,
        "updated_at_utc": utc_now(),
    }
    runs = [run for run in checkpoint.get("runs") or [] if isinstance(run, dict)]
    replaced = False
    updated_runs: list[dict[str, Any]] = []
    for run in runs:
        if run.get("run_id") == run_id:
            updated_runs.append(entry)
            replaced = True
        else:
            updated_runs.append(run)
    if not replaced:
        updated_runs.append(entry)
    checkpoint["runs"] = updated_runs
    return entry


__all__ = [
    "CHECKPOINT_JSON",
    "DuplicateCheck",
    "candidate_duplicate_check",
    "changes_hash",
    "checkpoint_path",
    "empty_checkpoint",
    "load_checkpoint",
    "record_checkpoint_run",
    "save_checkpoint",
]
