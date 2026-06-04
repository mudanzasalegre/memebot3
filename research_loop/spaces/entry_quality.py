from __future__ import annotations

from research_loop.search_space import SearchSpace, validate_search_space

SPACE_NAME = "entry_quality"
TARGET_LANES = ["pump_early_sniper_research", "research_rank_canary", "paper_exploration"]

ENTRY_QUALITY_PARAMETERS = {
    "RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE": [68, 70, 72, 75],
    "RESEARCH_RANK_CANARY_PRIORITY_MIN_TXNS_5M": [800, 1000, 1200],
    "SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M": [80, 100, 120],
    "SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_RANK": [65, 70, 75],
    "PAPER_IDLE_AFTER_HOURS": [2, 3, 4],
    "PAPER_IDLE_AMOUNT_SOL": [0.001, 0.002, 0.003],
    "PAPER_IDLE_MAX_DAILY_BUYS": [2, 3, 5],
}


def build_space() -> SearchSpace:
    return SearchSpace(
        name=SPACE_NAME,
        parameters={key: list(values) for key, values in ENTRY_QUALITY_PARAMETERS.items()},
        target_lanes=list(TARGET_LANES),
        hypothesis="Improve entry quality across rank canary, sniper momentum and idle paper exploration.",
        expected_effect={
            "increase_pnl": True,
            "increase_win_rate": True,
            "increase_moonshot_capture": True,
            "reduce_severe_losses": True,
        },
        risk_notes=["paper only", "entry thresholds and capped paper exploration only"],
    )


def optimization_targets() -> list[str]:
    return [
        "win_rate_pct",
        "median_pnl_pct",
        "rank_canary_profitability",
        "sniper_research_profitability",
        "idle_no_buy_hours",
    ]


def safety_caps_ok() -> bool:
    return validate_search_space(build_space()).ok


__all__ = [
    "ENTRY_QUALITY_PARAMETERS",
    "SPACE_NAME",
    "TARGET_LANES",
    "build_space",
    "optimization_targets",
    "safety_caps_ok",
]
