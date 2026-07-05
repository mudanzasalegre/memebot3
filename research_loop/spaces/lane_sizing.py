from __future__ import annotations

from research_loop.search_space import SearchSpace, validate_search_space

SPACE_NAME = "lane_sizing"
TARGET_LANES = [
    "research_rank_canary",
    "pump_early_sniper_research",
    "pump_early_sniper_research_micro_fallback",
    "shadow_followup_micro",
    "pump_early_moonshot_micro_lottery",
    "paper_exploration",
    "paper_bootstrap",
]

LANE_SIZING_PARAMETERS = {
    "RESEARCH_RANK_CANARY_SIZE_SOL": [0.01, 0.02, 0.03],
    "RESEARCH_RANK_CANARY_PRIORITY_SIZE_SOL": [0.01, 0.02, 0.03],
    "RESEARCH_RANK_CANARY_PAPER_NORMAL_SIZE_SOL": [0.005, 0.01, 0.02],
    "RESEARCH_RANK_CANARY_PULLBACK_TAIL_AMOUNT_SOL": [0.003, 0.005, 0.01, 0.02],
    "SNIPER_RESEARCH_SIZE_SOL": [0.005, 0.01, 0.02],
    "SNIPER_RESEARCH_MOMENTUM_SIZE_SOL": [0.005, 0.01, 0.02],
    "SNIPER_RESEARCH_DEEP_REVERSAL_SIZE_SOL": [0.005, 0.01, 0.02],
    "SNIPER_RESEARCH_MICRO_FALLBACK_AMOUNT_SOL": [0.003, 0.005, 0.01, 0.02],
    "LATE_MOMENTUM_MICRO_AMOUNT_SOL": [0.003, 0.005, 0.01, 0.02],
    "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL": [0.003, 0.005, 0.01, 0.02],
    "MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL": [0.001, 0.003, 0.005, 0.01, 0.02],
    "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_AMOUNT_SOL": [0.0005, 0.001, 0.002],
    "MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_AMOUNT_SOL": [0.0002, 0.0003, 0.0005],
    "PAPER_IDLE_AMOUNT_SOL": [0.02, 0.05, 0.1],
    "PAPER_EXPLORATION_AMOUNT_SOL": [0.02, 0.05, 0.1],
    "PAPER_BOOTSTRAP_AMOUNT_SOL": [0.02, 0.05, 0.1],
    "PAPER_BOOTSTRAP_MAX_AMOUNT_SOL": [0.05, 0.1],
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
        optimization_targets=optimization_targets(),
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
