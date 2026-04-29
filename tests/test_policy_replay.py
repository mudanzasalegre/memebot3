from __future__ import annotations

import json

from backtest.policy_replay import build_policy_replay


def test_policy_replay_combined_caps_severe_loss(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(
        json.dumps({"address": "A", "pnl_pct": -60, "exit_reason": "ADVERSE_TICK", "peak_pnl_pct": 0}) + "\n",
        encoding="utf-8",
    )
    report = build_policy_replay(tmp_path)
    assert report["combined_v1"]["avg_pnl"] > report["current"]["avg_pnl"]
