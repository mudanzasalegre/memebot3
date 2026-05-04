from __future__ import annotations

import json

from backtest.policy_replay import build_policy_replay, build_post_adjustment_policy_replay, write_post_adjustment_policy_replay


def test_policy_replay_combined_caps_severe_loss(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(
        json.dumps(
            {
                "address": "A",
                "entry_lane": "pump_early_green_candle_sniper",
                "pnl_pct": -60,
                "exit_reason": "ADVERSE_TICK",
                "peak_pnl_pct": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = build_policy_replay(tmp_path)
    assert report["combined_v1"]["avg_pnl"] > report["current"]["avg_pnl"]
    assert report["risk_guard"]["avg_pnl"] > report["current"]["avg_pnl"]
    assert "liq_guard" in report
    assert "research_rank_canary" in report
    assert report["current"]["lane_policy_category_breakdown"]["green_sniper_pure"]["trades"] == 1


def test_post_adjustment_policy_replay_compares_block2_policies(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    rows = [
        {
            "address": "GREEN",
            "entry_lane": "pump_early_green_candle_sniper",
            "pnl_pct": -60,
            "exit_reason": "ADVERSE_TICK",
            "peak_pnl_pct": 0,
        },
        {
            "address": "LATE",
            "entry_lane": "pump_early_late_momentum_watch",
            "pnl_pct": -30,
            "exit_reason": "NO_PUMP_EXIT",
            "peak_pnl_pct": 0,
        },
        {
            "address": "DUMP",
            "entry_lane": "pump_early_research_rank_canary",
            "pnl_pct": -40,
            "exit_reason": "EARLY_DUMP_CUT",
            "peak_pnl_pct": 0,
        },
        {
            "address": "RUN",
            "entry_lane": "pump_early_research_rank_canary",
            "pnl_pct": 20,
            "exit_reason": "POST_PARTIAL_TRAILING",
            "peak_pnl_pct": 100,
        },
    ]
    (metrics / "candidate_outcomes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    report = build_post_adjustment_policy_replay(tmp_path)
    policies = report["policies"]

    assert set(policies) >= {
        "baseline_48h",
        "research_rank_priority",
        "green_sniper_shadow_first",
        "green_sniper_restricted",
        "late_momentum_research_only",
        "post_partial_protected",
        "early_dump_candidates",
        "combined_adjusted_v1",
    }
    assert policies["green_sniper_shadow_first"]["total_pnl"] > policies["baseline_48h"]["total_pnl"]
    assert policies["late_momentum_research_only"]["total_pnl"] > policies["baseline_48h"]["total_pnl"]
    assert policies["early_dump_candidates"]["total_pnl"] > policies["baseline_48h"]["total_pnl"]
    assert policies["post_partial_protected"]["total_pnl"] > policies["baseline_48h"]["total_pnl"]
    assert policies["combined_adjusted_v1"]["severe_loss_count"] < policies["baseline_48h"]["severe_loss_count"]


def test_write_post_adjustment_policy_replay_outputs_json_and_docs(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(
        json.dumps({"address": "A", "pnl_pct": 10, "peak_pnl_pct": 20}) + "\n",
        encoding="utf-8",
    )

    write_post_adjustment_policy_replay(tmp_path)

    assert (metrics / "post_adjustment_policy_replay.json").exists()
    assert (tmp_path / "docs" / "POST_ADJUSTMENT_REPLAY.md").exists()
