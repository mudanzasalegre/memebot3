from __future__ import annotations

import json

from analytics.funnel_attribution import build_funnel_attribution


def test_late_funnel_is_not_final_blocker(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    rows = [
        {"address": "A", "stage": "late_funnel", "reason": "late_funnel", "ts_utc": "1"},
        {"address": "A", "stage": "soft_score", "reason": "soft_score", "ts_utc": "2"},
    ]
    (metrics / "candidate_outcomes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    (metrics / "runtime_events.jsonl").write_text("", encoding="utf-8")
    result = build_funnel_attribution(tmp_path)[0]
    assert result["final_blocking_reason"] == "soft_score"
    assert result["final_blocking_reason"] != "late_funnel"


def test_buy_wins_final_state(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(json.dumps({"address": "A", "reason": "reject"}), encoding="utf-8")
    (metrics / "runtime_events.jsonl").write_text(json.dumps({"address": "A", "event_type": "buy"}), encoding="utf-8")
    result = build_funnel_attribution(tmp_path)[0]
    assert result["final_state"] == "bought"
