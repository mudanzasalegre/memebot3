from __future__ import annotations

from research_loop.search_space import SearchSpace, validate_search_space

SPACE_NAME = "late_momentum"
TARGET_LANES = ["late_momentum_micro"]

LATE_MOMENTUM_PARAMETERS = {
    "LATE_MOMENTUM_MICRO_AMOUNT_SOL": [0.003, 0.005, 0.01, 0.02],
    "LATE_MOMENTUM_WATCH_MIN_PRICE5M": [250, 300, 400],
    "LATE_MOMENTUM_WATCH_MAX_PRICE5M": [650, 750, 1000],
    "LATE_MOMENTUM_WATCH_MIN_RANK_SCORE": [45, 55, 65],
    "LATE_MOMENTUM_WATCH_MIN_TXNS_5M": [150, 300, 500],
    "LATE_MOMENTUM_WATCH_MIN_LIQUIDITY_USD": [1500, 2000, 5000],
    "LATE_MOMENTUM_WATCH_MAX_PRICE_IMPACT_PCT": [10, 12, 18],
    "LATE_MOMENTUM_WATCH_MAX_OPEN_PAPER": [1, 2],
}


def build_space() -> SearchSpace:
    return SearchSpace(
        name=SPACE_NAME,
        parameters={key: list(values) for key, values in LATE_MOMENTUM_PARAMETERS.items()},
        target_lanes=list(TARGET_LANES),
        hypothesis="Improve late momentum micro entries with bounded buy quotas.",
        expected_effect={
            "increase_pnl": True,
            "increase_win_rate": True,
            "increase_moonshot_capture": True,
            "reduce_severe_losses": True,
        },
        optimization_targets=optimization_targets(),
        risk_notes=["paper only", "late momentum live remains disabled"],
    )


def optimization_targets() -> list[str]:
    return [
        "late_momentum_micro_profitability",
        "win_rate_pct",
        "median_pnl_pct",
        "severe_loss_count",
    ]


def safety_caps_ok() -> bool:
    return validate_search_space(build_space()).ok


__all__ = [
    "LATE_MOMENTUM_PARAMETERS",
    "SPACE_NAME",
    "TARGET_LANES",
    "build_space",
    "optimization_targets",
    "safety_caps_ok",
]
