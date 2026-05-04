from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from api.services.policy import (
    get_decision_ledger_envelope,
    get_policy_replay_envelope,
    get_policy_safety_envelope,
)
from api.settings import get_settings


def _tmp_settings(tmp_path: Path):
    data = tmp_path / "data"
    metrics = data / "metrics"
    runtime = data / "runtime"
    features = tmp_path / "features"
    for path in (metrics, runtime, features, tmp_path / "strategy_proposals" / "candidates", tmp_path / "ml" / "models"):
        path.mkdir(parents=True, exist_ok=True)
    return replace(
        get_settings(),
        project_root=tmp_path,
        data_dir=data,
        runtime_dir=runtime,
        metrics_dir=metrics,
        features_dir=features,
        db_path=tmp_path / "missing.sqlite",
        runtime_events_path=metrics / "runtime_events.jsonl",
        research_events_path=metrics / "research_events.jsonl",
        research_scorecard_json=metrics / "research_scorecard.json",
        research_thresholds_json=metrics / "research_thresholds.json",
        recommended_threshold_json=metrics / "recommended_threshold.json",
        train_status_json=metrics / "train_status.json",
        dataset_quality_json=metrics / "dataset_quality.json",
        paper_portfolio_path=data / "paper_portfolio.json",
        post_partial_experiment_json=metrics / "post_partial_experiment.json",
    )


def test_policy_api_service_handles_empty_workspace(tmp_path: Path) -> None:
    settings = _tmp_settings(tmp_path)

    safety = get_policy_safety_envelope(settings)
    replay = get_policy_replay_envelope(settings)
    ledger = get_decision_ledger_envelope(settings)

    assert safety.data["gates"]
    assert replay.data["policies"]
    assert ledger.data["items"] == []


def test_policy_api_decision_ledger_tail(tmp_path: Path) -> None:
    settings = _tmp_settings(tmp_path)
    ledger_path = settings.metrics_dir / "decision_ledger.jsonl"
    ledger_path.write_text(
        json.dumps(
            {
                "decision_id": "d1",
                "timestamp": "2026-01-01T00:00:00Z",
                "address": "A",
                "lane": "green",
                "decision": "shadow",
                "reason": "test",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    ledger = get_decision_ledger_envelope(settings)

    assert ledger.data["summary"]["by_action"]["shadow"] == 1
    assert ledger.data["items"][0]["decision_id"] == "d1"
