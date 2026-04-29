from __future__ import annotations

import json

from analytics.sniper_audit import build_sniper_audit


def test_sniper_audit_tolerates_minimal_data(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "runtime_events.jsonl").write_text(json.dumps({"event": "hot_queue_add", "source": "pumpfun"}) + "\n", encoding="utf-8")
    (metrics / "candidate_outcomes.jsonl").write_text(json.dumps({"action": "rejected", "reason": "no_route"}) + "\n", encoding="utf-8")
    report = build_sniper_audit(tmp_path)
    assert report["total_candidates_seen"] == 2
    assert report["pumpfun_candidates_seen"] == 1
    assert report["rejected_by_reason"]["no_route"] == 1
