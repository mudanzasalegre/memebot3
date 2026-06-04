from __future__ import annotations

from research_loop.search_space import SearchSpace, get_search_space, validate_search_space

SPACE_NAME = "runner_ladder"
TARGET_LANES = ["runner_exit", "pump_early_pumpswap_profit"]


def build_space() -> SearchSpace:
    return get_search_space(SPACE_NAME)


def optimization_targets() -> list[str]:
    return [
        "runner_capture_ratio",
        "realized_pnl_on_runners",
        "giveback_pct",
        "total_pnl_usd",
    ]


def safety_caps_ok() -> bool:
    return validate_search_space(build_space()).ok


__all__ = ["SPACE_NAME", "TARGET_LANES", "build_space", "optimization_targets", "safety_caps_ok"]
