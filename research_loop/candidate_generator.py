from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any, Callable

from research_loop.experiment_schema import validate_candidate_policy
from research_loop.paths import project_root
from research_loop.safety import validate_candidate_safety
from research_loop.search_space import SearchSpace, get_search_space, iter_grid, validate_search_space
from research_loop.spaces import entry_quality, lane_sizing, late_momentum, moonshot_micro, rank_canary, runner_exit, shadow_followup

GENERATION_MODES = {"grid", "random", "seeded_random", "local_search", "bandit_suggested"}
SPECIALIZED_BUILDERS: dict[str, Callable[[], SearchSpace]] = {
    "rank_canary": rank_canary.build_space,
    "shadow_followup": shadow_followup.build_space,
    "shadow_followup_micro": shadow_followup.build_space,
    "moonshot_micro": moonshot_micro.build_space,
    "runner_exit": runner_exit.build_space,
    "runner_ladder": runner_exit.build_space,
    "entry_quality": entry_quality.build_space,
    "late_momentum": late_momentum.build_space,
    "lane_sizing": lane_sizing.build_space,
}


class CandidateGenerationError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-").lower()
    return cleaned or "candidate"


def load_generation_space(space_name: str) -> SearchSpace:
    normalized = _safe_id(space_name)
    builder = SPECIALIZED_BUILDERS.get(normalized)
    if builder is not None:
        return builder()
    return get_search_space(space_name)


def _changes_hash(space_name: str, changes: dict[str, Any], index: int, seed: int | None) -> str:
    payload = {
        "space": space_name,
        "changes": changes,
        "index": index,
        "seed": seed,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:10]


def proposal_id_for(space_name: str, changes: dict[str, Any], *, index: int, seed: int | None) -> str:
    seed_part = "grid" if seed is None else f"s{seed}"
    return f"ar_{_safe_id(space_name)}_{seed_part}_{index:04d}_{_changes_hash(space_name, changes, index, seed)}"


def _build_candidate_policy(
    space: SearchSpace,
    changes: dict[str, Any],
    *,
    index: int,
    seed: int | None,
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    policy = {
        "proposal_id": proposal_id_for(space.name, changes, index=index, seed=seed),
        "created_at_utc": created_at_utc or utc_now(),
        "experiment_type": "replay",
        "hypothesis": space.hypothesis,
        "target_lanes": list(space.target_lanes),
        "changes": dict(changes),
        "expected_effect": dict(space.expected_effect),
        "required_gates": ["replay_positive", "api_budget_ok"],
        "api_budget_sensitive": True,
        "live_allowed": False,
        "risk_notes": list(space.risk_notes),
    }
    validate_candidate_policy(policy)
    safety = validate_candidate_safety(policy)
    if not safety.ok:
        raise CandidateGenerationError(";".join(safety.errors))
    return policy


def _random_changes(space: SearchSpace, rng: random.Random) -> dict[str, Any]:
    return {key: rng.choice(values) for key, values in space.parameters.items()}


def _middle_value(values: list[Any]) -> Any:
    return values[len(values) // 2]


def _local_search_changes(space: SearchSpace, index: int) -> dict[str, Any]:
    keys = list(space.parameters)
    base = {key: _middle_value(space.parameters[key]) for key in keys}
    if not keys:
        return base
    key = keys[index % len(keys)]
    values = space.parameters[key]
    base[key] = values[(index // len(keys)) % len(values)]
    return base


def _candidate_changes(
    space: SearchSpace,
    *,
    mode: str,
    n: int,
    seed: int | None,
) -> list[dict[str, Any]]:
    if n <= 0:
        return []
    if mode == "grid":
        out: list[dict[str, Any]] = []
        for changes in iter_grid(space):
            out.append(changes)
            if len(out) >= n:
                break
        return out
    if mode == "local_search":
        return [_local_search_changes(space, index) for index in range(n)]
    if mode == "bandit_suggested":
        rng = random.Random(0 if seed is None else seed)
        return [_local_search_changes(space, index) if index % 2 == 0 else _random_changes(space, rng) for index in range(n)]
    if mode in {"random", "seeded_random"}:
        rng = random.Random(0 if seed is None else seed) if mode == "seeded_random" else random.Random(seed)
        return [_random_changes(space, rng) for _ in range(n)]
    raise CandidateGenerationError(f"unknown_generation_mode:{mode}")


def generate_candidate_policies(
    *,
    space_name: str,
    n: int,
    mode: str = "seeded_random",
    seed: int | None = None,
    created_at_utc: str | None = None,
) -> list[dict[str, Any]]:
    mode = str(mode).strip()
    if mode not in GENERATION_MODES:
        raise CandidateGenerationError(f"unknown_generation_mode:{mode}")
    space = load_generation_space(space_name)
    validation = validate_search_space(space)
    if not validation.ok:
        raise CandidateGenerationError(";".join(validation.errors))
    changes_list = _candidate_changes(space, mode=mode, n=n, seed=seed)
    return [
        _build_candidate_policy(
            space,
            changes,
            index=index,
            seed=seed,
            created_at_utc=created_at_utc,
        )
        for index, changes in enumerate(changes_list)
    ]


def write_candidate_policies(
    candidates: list[dict[str, Any]],
    *,
    root: str | Path | None = None,
) -> list[Path]:
    resolved_root = project_root(root)
    out_dir = resolved_root / "strategy_proposals" / "candidates"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for candidate in candidates:
        validate_candidate_policy(candidate)
        path = out_dir / f"{candidate['proposal_id']}.json"
        path.write_text(json.dumps(candidate, indent=2, sort_keys=True, default=str), encoding="utf-8")
        paths.append(path)
    return paths


def generate_research_candidates(
    *,
    space_name: str,
    n: int,
    mode: str = "seeded_random",
    seed: int | None = None,
    root: str | Path | None = None,
    write: bool = True,
    created_at_utc: str | None = None,
) -> list[dict[str, Any]]:
    candidates = generate_candidate_policies(
        space_name=space_name,
        n=n,
        mode=mode,
        seed=seed,
        created_at_utc=created_at_utc,
    )
    if write:
        write_candidate_policies(candidates, root=root)
    return candidates


__all__ = [
    "CandidateGenerationError",
    "GENERATION_MODES",
    "SPECIALIZED_BUILDERS",
    "generate_candidate_policies",
    "generate_research_candidates",
    "load_generation_space",
    "proposal_id_for",
    "write_candidate_policies",
]
