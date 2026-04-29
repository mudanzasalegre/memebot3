from __future__ import annotations

import json

from analytics.runner_capture import build_runner_capture


def test_runner_capture_counts_gt_100(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(
        json.dumps({"address": "A", "pnl_pct": 50, "max_pnl_pct_seen": 200}) + "\n",
        encoding="utf-8",
    )
    report = build_runner_capture(tmp_path)
    assert report["summary"]["gt_100"]["count"] == 1
