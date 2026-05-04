from __future__ import annotations

import json

from analytics.trade_diagnostics import build_trade_diagnostics


def test_trade_diagnostics_groups_by_exit_and_lane(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    rows = [
        {"address": "A", "entry_lane": "pump_early_green_candle_sniper", "exit_reason": "ADVERSE_TICK", "pnl_pct": -30},
        {"address": "B", "entry_lane": "pump_early_green_candle_sniper", "exit_reason": "POST_PARTIAL_TRAILING", "pnl_pct": 80},
    ]
    (metrics / "candidate_outcomes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    report = build_trade_diagnostics(tmp_path)
    assert report["summary"]["trades"] == 2
    assert report["groups"]["entry_lane:pump_early_green_candle_sniper"]["severe_loss_count"] == 1
    assert report["groups"]["lane_policy_category:green_sniper_shadow"]["trades"] == 2
    assert report["groups"]["exit_reason:ADVERSE_TICK"]["avg_pnl"] == -30
