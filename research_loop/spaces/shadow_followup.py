from __future__ import annotations

from research_loop.search_space import SearchSpace, get_search_space, validate_search_space

SPACE_NAME = "shadow_followup_micro"
TARGET_LANES = ["shadow_followup_micro"]


def build_space() -> SearchSpace:
    return get_search_space(SPACE_NAME)


def optimization_targets() -> list[str]:
    return [
        "shadow_followup_success_rate",
        "observed_peak_after_seen_50",
        "candidate_partial_50",
        "avg_pnl_pct",
        "severe_loss_count",
    ]


def safety_caps_ok() -> bool:
    return validate_search_space(build_space()).ok


__all__ = ["SPACE_NAME", "TARGET_LANES", "build_space", "optimization_targets", "safety_caps_ok"]
