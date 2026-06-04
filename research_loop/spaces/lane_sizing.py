from __future__ import annotations

from research_loop.search_space import SearchSpace, validate_search_space

SPACE_NAME = "lane_sizing"
TARGET_LANES = [
    "research_rank_canary",
    "pump_early_sniper_research",
    "shadow_followup_micro",
    "pump_early_moonshot_micro_lottery",
    "paper_exploration",
]

LANE_SIZING_PARAMETERS = {
    "RESEARCH_RANK_CANARY_SIZE_SOL": [0.01, 0.02, 0.03],
    "SNIPER_RESEARCH_SIZE_SOL": [0.001, 0.002, 0.003, 0.005],
    "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL": [0.001, 0.002, 0.003],
    "MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL": [0.0005, 0.001, 0.002, 0.005],
    "PAPER_EXPLORATION_AMOUNT_SOL": [0.001, 0.002, 0.003, 0.005],
}


def build_space() -> SearchSpace:
    return SearchSpace(
        name=SPACE_NAME,
        parameters={key: list(values) for key, values in LANE_SIZING_PARAMETERS.items()},
        target_lanes=list(TARGET_LANES),
        hypothesis="Tune paper lane sizing while respecting all safety caps.",
        expected_effect={
            "increase_pnl": True,
            "increase_win_rate": False,
            "increase_moonshot_capture": True,
            "reduce_severe_losses": False,
        },
        risk_notes=["paper only", "safety caps enforced per lane"],
    )


def optimization_targets() -> list[str]:
    return [
        "total_pnl_usd",
        "median_pnl_pct",
        "runner_capture_ratio",
        "severe_loss_count",
        "overtrading_count",
    ]


def safety_caps_ok() -> bool:
    return validate_search_space(build_space()).ok


__all__ = [
    "LANE_SIZING_PARAMETERS",
    "SPACE_NAME",
    "TARGET_LANES",
    "build_space",
    "optimization_targets",
    "safety_caps_ok",
]
