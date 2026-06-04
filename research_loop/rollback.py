from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_loop.paths import project_root, research_runs_dir
from research_loop.policy_promoter import PAPER_PROFILE_PREFIX

STATUS_REJECTED_PAPER = "rejected_paper"


class PaperRollbackError(RuntimeError):
    pass


@dataclass(frozen=True)
class RollbackResult:
    run_id: str
    status: str
    restored: bool
    profile_path: Path | None
    backup_path: Path | None
    rollback_report_path: Path
    reason: str
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "restored": self.restored,
            "profile_path": str(self.profile_path) if self.profile_path else None,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "rollback_report_path": str(self.rollback_report_path),
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _paper_forward_root(root: Path) -> Path:
    return research_runs_dir(root) / "paper_forward"


def _resolve_run_dir(run_id_or_dir: str | Path, root: Path) -> Path:
    candidate = Path(run_id_or_dir)
    if candidate.exists() or candidate.is_absolute() or len(candidate.parts) > 1:
        return candidate
    return _paper_forward_root(root) / str(run_id_or_dir)


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _path_from_state(value: Any) -> Path | None:
    if not value:
        return None
    return Path(str(value))


def _ensure_paper_profile(profile_path: Path, root: Path) -> None:
    profiles_dir = (root / "config" / "profiles").resolve()
    resolved_profile = profile_path.resolve()
    try:
        resolved_profile.relative_to(profiles_dir)
    except ValueError as exc:
        raise PaperRollbackError("profile_path_outside_config_profiles") from exc
    if not profile_path.name.startswith(PAPER_PROFILE_PREFIX) or "live" in profile_path.name.lower():
        raise PaperRollbackError("rollback_target_must_be_paper_research_candidate")


def rollback_paper_candidate(
    run_id_or_dir: str | Path,
    *,
    root: str | Path | None = None,
    reason: str = "candidate_degraded",
) -> RollbackResult:
    resolved_root = project_root(root)
    run_dir = _resolve_run_dir(run_id_or_dir, resolved_root)
    state_path = run_dir / "paper_forward_state.json"
    state = _read_json(state_path)
    if not isinstance(state, dict):
        raise PaperRollbackError(f"paper_forward_state_missing:{state_path}")

    promotion = state.get("promotion") if isinstance(state.get("promotion"), dict) else {}
    profile_path = _path_from_state(promotion.get("profile_path") or state.get("paper_profile_path"))
    backup_path = _path_from_state(promotion.get("backup_path"))
    warnings: list[str] = []
    restored = False
    if profile_path is None:
        warnings.append("missing_profile_path")
    else:
        _ensure_paper_profile(profile_path, resolved_root)
        if backup_path is not None and backup_path.exists():
            shutil.copy2(backup_path, profile_path)
            restored = True
        else:
            warnings.append("no_previous_profile_backup")

    state["status"] = STATUS_REJECTED_PAPER
    state["rollback"] = {
        "reason": reason,
        "restored": restored,
        "profile_path": str(profile_path) if profile_path else None,
        "backup_path": str(backup_path) if backup_path else None,
        "rolled_back_at_utc": utc_now(),
        "warnings": warnings,
    }
    _write_json(state_path, state)

    rollback_report_path = run_dir / "rollback_report.json"
    result = RollbackResult(
        run_id=str(state.get("run_id") or run_dir.name),
        status=STATUS_REJECTED_PAPER,
        restored=restored,
        profile_path=profile_path,
        backup_path=backup_path,
        rollback_report_path=rollback_report_path,
        reason=reason,
        warnings=warnings,
    )
    _write_json(rollback_report_path, result.as_dict())
    return result


__all__ = [
    "PaperRollbackError",
    "RollbackResult",
    "STATUS_REJECTED_PAPER",
    "rollback_paper_candidate",
]
