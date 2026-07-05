from __future__ import annotations

from research_loop.search_space import SearchSpace, validate_search_space

SPACE_NAME = "entry_quality"
TARGET_LANES = ["pump_early_sniper_research", "research_rank_canary", "paper_exploration"]

ENTRY_QUALITY_PARAMETERS = {
    "RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE": [68, 70, 72, 75],
    "RESEARCH_RANK_CANARY_PRIORITY_MIN_TXNS_5M": [800, 1000, 1200, 1500],
    "RESEARCH_RANK_CANARY_PRIORITY_MIN_LIQUIDITY_USD": [15000, 20000, 25000],
    "RESEARCH_RANK_CANARY_PRIORITY_MIN_PRICE5M": [40, 50, 60],
    "RESEARCH_RANK_CANARY_PRIORITY_MAX_PRICE5M": [100, 120, 150],
    "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_RANK_SCORE": [58, 62, 65],
    "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_TXNS_5M": [150, 300, 500],
    "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_LIQUIDITY_USD": [8000, 12000, 15000],
    "SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M": [80, 100, 120],
    "SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M": [140, 150, 180],
    "SNIPER_RESEARCH_MOMENTUM_MIN_LIQUIDITY_USD": [10000, 15000, 25000],
    "SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M": [300, 500, 800],
    "SNIPER_RESEARCH_MOMENTUM_MAX_MCAP_USD": [50000, 70000, 120000],
    "SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_RANK": [65, 70, 75],
    "SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_TXNS_5M": [800, 1200, 1600],
    "SNIPER_RESEARCH_DEEP_REVERSAL_MIN_PRICE5M": [-95, -90, -75],
    "SNIPER_RESEARCH_DEEP_REVERSAL_MAX_PRICE5M": [-60, -50, -35],
    "SNIPER_RESEARCH_DEEP_REVERSAL_MIN_TXNS_5M": [300, 500, 800],
    "PAPER_IDLE_AFTER_HOURS": [0],
    "PAPER_IDLE_AMOUNT_SOL": [0.02, 0.05, 0.1],
    "PAPER_EXPLORATION_MAX_OPEN": [0],
    "PAPER_IDLE_MAX_DAILY_BUYS": [0],
    "PAPER_BOOTSTRAP_MAX_OPEN": [0],
    "PAPER_BOOTSTRAP_MAX_DAILY_BUYS": [0],
    "PAPER_BOOTSTRAP_MIN_SECONDS_BETWEEN_BUYS": [0],
}


def build_space() -> SearchSpace:
    return SearchSpace(
        name=SPACE_NAME,
        parameters={key: list(values) for key, values in ENTRY_QUALITY_PARAMETERS.items()},
        target_lanes=list(TARGET_LANES),
        hypothesis="Improve entry quality across rank canary, sniper momentum and idle paper exploration without buy quotas.",
        expected_effect={
            "increase_pnl": True,
            "increase_win_rate": True,
            "increase_moonshot_capture": True,
            "reduce_severe_losses": True,
        },
        optimization_targets=optimization_targets(),
        risk_notes=["paper only", "entry thresholds only; buy quotas remain unlimited"],
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
