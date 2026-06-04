from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SPACES = (
    "rank_canary",
    "shadow_followup",
    "moonshot_micro",
    "runner_exit",
    "sniper_momentum",
    "paper_exploration",
    "late_momentum",
    "lane_sizing",
)


@dataclass(frozen=True)
class BanditArmStats:
    space: str
    pulls: int
    total_reward: float
    avg_reward: float
    best_reward: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "space": self.space,
            "pulls": self.pulls,
            "total_reward": self.total_reward,
            "avg_reward": self.avg_reward,
            "best_reward": self.best_reward,
        }


@dataclass(frozen=True)
class BanditSuggestion:
    spaces: list[str]
    stats: dict[str, BanditArmStats] = field(default_factory=dict)
    mode: str = "epsilon_greedy"

    def as_dict(self) -> dict[str, Any]:
        return {
            "spaces": list(self.spaces),
            "mode": self.mode,
            "stats": {key: value.as_dict() for key, value in self.stats.items()},
        }


def reward_from_scoreboard_entry(entry: dict[str, Any]) -> float:
    objective_delta = float(entry.get("objective_score") or 0.0)
    risk_penalty = 0.0
    risk_penalty += max(0.0, float(entry.get("severe_loss_delta") or 0.0)) * 40.0
    risk_penalty += max(0.0, float(entry.get("liquidity_crush_delta") or 0.0)) * 35.0
    risk_penalty += max(0.0, float(entry.get("adverse_tick_delta") or 0.0)) * 20.0
    api_delta = entry.get("api_budget_delta") if isinstance(entry.get("api_budget_delta"), dict) else {}
    api_penalty = 0.0
    if isinstance(api_delta, dict):
        api_penalty += max(0.0, float(api_delta.get("api_429_count_delta") or 0.0)) * 50.0
        api_penalty += max(0.0, float(api_delta.get("provider_degraded_minutes_delta") or 0.0)) * 25.0
    return objective_delta - risk_penalty - api_penalty


def space_from_proposal_id(proposal_id: str) -> str | None:
    raw = str(proposal_id or "")
    match = re.match(r"^ar_([a-z0-9_]+?)_(?:s\d+|grid)_\d{4}_", raw)
    if match:
        return match.group(1)
    if raw.startswith("ar_"):
        body = raw[3:]
        for suffix in ("_smoke", "_test"):
            body = body.removesuffix(suffix)
        return body or None
    return None


def _entry_space(entry: dict[str, Any]) -> str | None:
    explicit = entry.get("space")
    if explicit:
        return str(explicit)
    return space_from_proposal_id(str(entry.get("proposal_id") or ""))


def summarize_space_rewards(
    scoreboard_entries: list[dict[str, Any]],
    *,
    spaces: tuple[str, ...] = DEFAULT_SPACES,
) -> dict[str, BanditArmStats]:
    rewards: dict[str, list[float]] = {space: [] for space in spaces}
    for entry in scoreboard_entries:
        space = _entry_space(entry)
        if space == "runner_ladder":
            space = "runner_exit"
        if space == "shadow_followup_micro":
            space = "shadow_followup"
        if space not in rewards:
            continue
        rewards[space].append(reward_from_scoreboard_entry(entry))
    stats: dict[str, BanditArmStats] = {}
    for space, values in rewards.items():
        if not values:
            stats[space] = BanditArmStats(space=space, pulls=0, total_reward=0.0, avg_reward=0.0, best_reward=0.0)
            continue
        total = sum(values)
        stats[space] = BanditArmStats(
            space=space,
            pulls=len(values),
            total_reward=total,
            avg_reward=total / len(values),
            best_reward=max(values),
        )
    return stats


def suggest_spaces(
    scoreboard_entries: list[dict[str, Any]],
    *,
    n: int = 1,
    seed: int | None = None,
    epsilon: float = 0.2,
    spaces: tuple[str, ...] = DEFAULT_SPACES,
) -> BanditSuggestion:
    if n <= 0:
        return BanditSuggestion(spaces=[], stats={})
    rng = random.Random(0 if seed is None else seed)
    stats = summarize_space_rewards(scoreboard_entries, spaces=spaces)
    unexplored = [space for space in spaces if stats[space].pulls == 0]
    ranked = sorted(spaces, key=lambda space: (stats[space].avg_reward, -stats[space].pulls, space), reverse=True)
    chosen: list[str] = []
    for _ in range(n):
        if unexplored:
            choice = unexplored.pop(0)
        elif rng.random() < epsilon:
            choice = rng.choice(list(spaces))
        else:
            choice = ranked[0]
        chosen.append(choice)
    return BanditSuggestion(spaces=chosen, stats=stats)


__all__ = [
    "DEFAULT_SPACES",
    "BanditArmStats",
    "BanditSuggestion",
    "reward_from_scoreboard_entry",
    "space_from_proposal_id",
    "suggest_spaces",
    "summarize_space_rewards",
]
