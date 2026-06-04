from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_loop.experiment_schema import validate_candidate_policy
from research_loop.safety import load_safety_config, validate_candidate_safety

EXIT_MARKERS = ("TP", "STOP", "EXIT", "RUNNER", "FLOOR", "GIVEBACK", "MOONBAG")
SIZING_MARKERS = ("AMOUNT_SOL", "SIZE_SOL", "MAX_SIZE_SOL", "MAX_SOL")


def _load_candidate(path_or_dict: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(path_or_dict, dict):
        return dict(path_or_dict)
    path = Path(path_or_dict)
    return json.loads(path.read_text(encoding="utf-8"))


def _affected_by_key(key: str) -> str:
    upper = key.upper()
    if "MOONSHOT" in upper:
        return "moonshot_micro"
    if "SHADOW_FOLLOWUP" in upper:
        return "shadow_followup"
    if "RESEARCH_RANK_CANARY" in upper:
        return "rank_canary"
    if "LATE_MOMENTUM" in upper:
        return "late_momentum"
    if "SNIPER" in upper:
        return "sniper"
    if "PAPER_EXPLORATION" in upper or "PAPER_IDLE" in upper:
        return "paper_exploration"
    if "BIRD_" in upper or "RUNNER" in upper:
        return "runner_exit"
    return "general"


def build_candidate_diff(candidate_policy: str | Path | dict[str, Any]) -> str:
    raw = _load_candidate(candidate_policy)
    policy = validate_candidate_policy(raw)
    safety = validate_candidate_safety(policy.to_dict())
    config = load_safety_config()
    protected_api_keys = {str(key).upper() for key in config.get("api_budget_protected_keys", [])}
    changes = policy.changes
    lanes = sorted(set(policy.target_lanes + [_affected_by_key(key) for key in changes]))
    exit_keys = [key for key in sorted(changes) if any(marker in key.upper() for marker in EXIT_MARKERS)]
    sizing_keys = [key for key in sorted(changes) if any(marker in key.upper() for marker in SIZING_MARKERS)]
    api_keys = [key for key in sorted(changes) if key.upper() in protected_api_keys]

    lines = [
        "# AutoResearch Candidate Diff",
        "",
        f"- proposal_id: `{policy.proposal_id}`",
        f"- experiment_type: `{policy.experiment_type}`",
        f"- live_allowed: `{str(policy.live_allowed).lower()}`",
        "",
        "## Env Changes",
        "",
        "| Key | Value |",
        "| --- | --- |",
    ]
    for key in sorted(changes):
        lines.append(f"| `{key}` | `{changes[key]}` |")

    lines.extend(
        [
            "",
            "## Lanes Affected",
            "",
            *(f"- `{lane}`" for lane in lanes),
            "",
            "## Exits Affected",
            "",
        ]
    )
    lines.extend([f"- `{key}`" for key in exit_keys] or ["- none"])
    lines.extend(["", "## Sizing Affected", ""])
    lines.extend([f"- `{key}`" for key in sizing_keys] or ["- none"])
    lines.extend(["", "## Risk Impact", ""])
    if safety.ok:
        lines.append("- safety: `ok`")
        lines.append("- risk caps: `within_configured_limits`")
    else:
        lines.append("- safety: `failed`")
        lines.extend(f"- `{error}`" for error in safety.errors)
    lines.extend(["", "## API Impact", ""])
    if api_keys:
        lines.extend(f"- protected API key changed: `{key}`" for key in api_keys)
    else:
        lines.append("- no protected API cadence/RPM keys changed")
    return "\n".join(lines).rstrip() + "\n"


def write_candidate_diff(
    candidate_policy: str | Path | dict[str, Any],
    *,
    run_dir: str | Path | None = None,
    output_path: str | Path | None = None,
) -> Path:
    if output_path is None:
        if run_dir is None:
            raise ValueError("run_dir or output_path is required")
        output = Path(run_dir) / "candidate_diff.md"
    else:
        output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_candidate_diff(candidate_policy), encoding="utf-8")
    return output


__all__ = ["build_candidate_diff", "write_candidate_diff"]
