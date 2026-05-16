from __future__ import annotations

import json

from analytics.runner_capture_ladder_report import build_runner_capture_ladder_report, write_runner_capture_ladder_report


def test_runner_capture_ladder_report_summarizes_runner_rows(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(
        json.dumps(
            {
                "address": "A",
                "entry_lane": "pump_early_sniper_research",
                "max_pnl_pct_seen": 300,
                "realized_pnl_pct": 40,
                "exit_reason": "POST_PARTIAL_TRAILING",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_runner_capture_ladder_report(tmp_path)

    assert report["summary"]["rows"] == 1
    assert report["by_peak_bucket"]["peak_300_plus"]["rows"] == 1
    assert report["top_runner_preview"][0]["partials_triggered"] == 4
    assert report["top_runner_preview"][0]["simulated_realized_pnl_pct"] > 40


def test_runner_capture_ladder_report_writes_json_and_docs(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(
        json.dumps({"address": "B", "max_pnl_pct_seen": 100, "realized_pnl_pct": 70}) + "\n",
        encoding="utf-8",
    )

    report = write_runner_capture_ladder_report(tmp_path)

    assert report["summary"]["rows"] == 1
    assert (metrics / "runner_capture_ladder_report.json").exists()
    assert (tmp_path / "docs" / "BIRD_RUNNER_MULTI_PARTIAL.md").exists()
