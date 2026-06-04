from __future__ import annotations

from research_loop.search_space import SearchSpace, get_search_space

SPACE_NAME = "rank_canary"
TARGET_LANES = ["pump_early_sniper_research", "research_rank_canary"]


def build_space() -> SearchSpace:
    return get_search_space(SPACE_NAME)


def optimization_targets() -> list[str]:
    return [
        "rank_canary_profitability",
        "win_rate_pct",
        "median_pnl_pct",
        "severe_loss_count",
    ]


__all__ = ["SPACE_NAME", "TARGET_LANES", "build_space", "optimization_targets"]
