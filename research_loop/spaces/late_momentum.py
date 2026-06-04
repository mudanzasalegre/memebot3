from __future__ import annotations

from research_loop.search_space import SearchSpace, validate_search_space

SPACE_NAME = "late_momentum"
TARGET_LANES = ["late_momentum_micro"]

LATE_MOMENTUM_PARAMETERS = {
    "LATE_MOMENTUM_MICRO_AMOUNT_SOL": [0.001, 0.002, 0.003],
    "LATE_MOMENTUM_MIN_PRICE5M": [80, 100, 120],
    "LATE_MOMENTUM_MAX_PRICE5M": [180, 250, 300],
    "LATE_MOMENTUM_MIN_TXNS": [80, 120, 200],
    "LATE_MOMENTUM_MIN_LIQ": [10000, 15000, 25000],
    "LATE_MOMENTUM_CONFIRMATION": [1, 2, 3],
}


def build_space() -> SearchSpace:
    return SearchSpace(
        name=SPACE_NAME,
        parameters={key: list(values) for key, values in LATE_MOMENTUM_PARAMETERS.items()},
        target_lanes=list(TARGET_LANES),
        hypothesis="Improve late momentum micro entries with capped paper-only sizing.",
        expected_effect={
            "increase_pnl": True,
            "increase_win_rate": True,
            "increase_moonshot_capture": True,
            "reduce_severe_losses": True,
        },
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
