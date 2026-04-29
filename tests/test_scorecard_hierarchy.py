from __future__ import annotations

import json

from analytics.scorecard import build_hierarchical_scorecard, sublane_allows_canary


def test_positive_sublane_canary_despite_other_rows(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    rows = [
        {"entry_lane": "pump_early_sniper_research", "pnl_pct": 10},
        {"entry_lane": "pump_early_sniper_research", "pnl_pct": 4},
        {"entry_lane": "pump_early_green_candle_sniper", "pnl_pct": -30},
    ]
    (metrics / "candidate_outcomes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    report = build_hierarchical_scorecard(tmp_path)
    assert sublane_allows_canary(report, lane="pump_early_sniper_research", min_trades=2)
