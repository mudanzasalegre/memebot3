from __future__ import annotations

from research_loop.search_space import SearchSpace, get_search_space, validate_search_space

SPACE_NAME = "moonshot_micro"
TARGET_LANES = ["pump_early_moonshot_micro_lottery"]


def build_space() -> SearchSpace:
    return get_search_space(SPACE_NAME)


def optimization_targets() -> list[str]:
    return [
        "moonshot_peak100_capture",
        "moonshot_peak500_capture",
        "moonshot_peak1000_capture",
        "moonshot_micro_tail_capture_ratio",
        "severe_loss_count",
    ]


def safety_caps_ok() -> bool:
    return validate_search_space(build_space()).ok


__all__ = ["SPACE_NAME", "TARGET_LANES", "build_space", "optimization_targets", "safety_caps_ok"]
