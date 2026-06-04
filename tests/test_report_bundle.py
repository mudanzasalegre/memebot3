from __future__ import annotations

import json

from research_loop.report_bundle import build_report_bundle


def test_report_bundle_works_with_empty_data(tmp_path) -> None:
    bundle = build_report_bundle(tmp_path)

    assert bundle["current_run"]["summary"]["placeholder"] is True
    assert bundle["historical"]["policy_replay"]["placeholder"] is True
    assert bundle["api_budget"]["sources"]["mode"] == "local_files_only"
    assert (tmp_path / "data" / "research_runs" / "report_bundle_latest.json").exists()


def test_report_bundle_separates_current_run_and_historical(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "current_run_summary.json").write_text('{"run_id":"current","closed_positions":2}', encoding="utf-8")
    (metrics / "policy_replay.json").write_text('{"current":{"trades":10}}', encoding="utf-8")

    bundle = build_report_bundle(tmp_path, include_api_budget=False)

    assert bundle["current_run"]["summary"]["run_id"] == "current"
    assert bundle["historical"]["policy_replay"]["current"]["trades"] == 10
    saved = json.loads((tmp_path / "data" / "research_runs" / "report_bundle_latest.json").read_text())
    assert saved["recommendation_context"]["source"] == "local_reports_only"
