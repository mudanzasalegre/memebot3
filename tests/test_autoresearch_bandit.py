from __future__ import annotations

from research_loop.bandit import reward_from_scoreboard_entry, space_from_proposal_id, suggest_spaces, summarize_space_rewards


def test_reward_subtracts_risk_and_api_penalties() -> None:
    reward = reward_from_scoreboard_entry(
        {
            "objective_score": 100.0,
            "severe_loss_delta": 1,
            "liquidity_crush_delta": 0,
            "adverse_tick_delta": 1,
            "api_budget_delta": {"api_429_count_delta": 1, "provider_degraded_minutes_delta": 2},
        }
    )

    assert reward == -60.0


def test_space_from_proposal_id_parses_generated_ids() -> None:
    assert space_from_proposal_id("ar_moonshot_micro_s42_0000_abcdef1234") == "moonshot_micro"
    assert space_from_proposal_id("ar_runner_ladder_grid_0001_abcdef1234") == "runner_ladder"


def test_summarize_space_rewards_computes_arm_stats() -> None:
    stats = summarize_space_rewards(
        [
            {"proposal_id": "ar_moonshot_micro_s1_0000_a", "objective_score": 10.0},
            {"proposal_id": "ar_moonshot_micro_s1_0001_b", "objective_score": 20.0},
        ]
    )

    assert stats["moonshot_micro"].pulls == 2
    assert stats["moonshot_micro"].avg_reward == 15.0


def test_suggest_spaces_prefers_unexplored_then_best_arm() -> None:
    entries = [
        {"proposal_id": "ar_rank_canary_s1_0000_a", "objective_score": 1.0},
        {"proposal_id": "ar_moonshot_micro_s1_0000_b", "objective_score": 10.0},
    ]
    unexplored = suggest_spaces(entries, n=1, seed=1, spaces=("rank_canary", "moonshot_micro", "lane_sizing"))
    explored = suggest_spaces(entries, n=1, seed=1, epsilon=0.0, spaces=("rank_canary", "moonshot_micro"))

    assert unexplored.spaces == ["lane_sizing"]
    assert explored.spaces == ["moonshot_micro"]
