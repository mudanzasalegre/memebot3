from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from api.main import create_app
from api.services.research import (
    get_research_api_budget_envelope,
    get_research_current_best_envelope,
    get_research_moonshot_progress_envelope,
    get_research_paper_forward_envelope,
    get_research_runs_envelope,
    get_research_scoreboard_envelope,
)
from api.settings import get_settings


def _tmp_settings(tmp_path: Path):
    data = tmp_path / "data"
    metrics = data / "metrics"
    runtime = data / "runtime"
    features = tmp_path / "features"
    for path in (
        metrics,
        runtime,
        features,
        data / "research_runs" / "runs",
        data / "research_runs" / "paper_forward",
        tmp_path / "strategy_proposals" / "candidates",
    ):
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


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _seed_research_artifacts(settings) -> None:
    research_runs = settings.data_dir / "research_runs"
    _write_json(
        research_runs / "scoreboard.json",
        {
            "generated_at_utc": "2026-06-04T00:00:00+00:00",
            "entries": [
                {
                    "run_id": "ar_run_1",
                    "proposal_id": "ar_candidate_1",
                    "status": "accepted_replay",
                    "objective_score": 12.5,
                    "total_pnl_delta": 4.0,
                    "runner_capture_delta": 0.2,
                    "evaluated_at_utc": "2026-06-04T01:00:00+00:00",
                },
                {
                    "run_id": "ar_run_2",
                    "proposal_id": "ar_candidate_2",
                    "status": "rejected",
                    "objective_score": -1.0,
                    "evaluated_at_utc": "2026-06-04T02:00:00+00:00",
                },
            ],
        },
    )
    run_dir = research_runs / "runs" / "ar_run_1"
    _write_json(
        run_dir / "candidate_policy.json",
        {
            "proposal_id": "ar_candidate_1",
            "live_allowed": False,
            "changes": {"MOONSHOT_MICRO_CONFIRMATION_PNL": "75"},
        },
    )
    _write_json(run_dir / "replay_metrics.json", {"total_pnl_usd": 14.0, "closed_trades": 6})
    _write_json(research_runs / "api_budget.json", {"gecko_429_count": 0, "birdeye_429_count": 1, "jupiter_rate_limit_count": 0})
    _write_json(settings.metrics_dir / "api_budget_report.json", {"gecko_429_count": 0, "birdeye_429_count": 1})
    _write_json(
        settings.metrics_dir / "moonshot_micro_lottery_report.json",
        {"candidates_seen": 9, "peak100_capture": 2},
    )
    _write_json(settings.metrics_dir / "runner_capture_ladder_report.json", {"runner_capture_ratio": 0.42})
    _write_json(settings.metrics_dir / "missed_pumps.json", {"missed_peak100_count": 3})
    paper_dir = research_runs / "paper_forward" / "paper_1"
    _write_json(
        paper_dir / "paper_forward_state.json",
        {
            "run_id": "paper_1",
            "status": "paper_forward_started",
            "started_at_utc": "2026-06-04T03:00:00+00:00",
            "paper_profile": "paper_research_candidate_ar_candidate_1",
            "promotion": {"proposal_id": "ar_candidate_1"},
        },
    )
    _write_json(
        paper_dir / "paper_forward_result.json",
        {
            "run_id": "paper_1",
            "status": "accepted_paper",
            "accepted": True,
            "objective": {"score": 7.0},
            "rejection_reasons": [],
        },
    )


def test_research_api_services_read_local_autoresearch_artifacts(tmp_path: Path) -> None:
    settings = _tmp_settings(tmp_path)
    _seed_research_artifacts(settings)

    scoreboard = get_research_scoreboard_envelope(settings)
    runs = get_research_runs_envelope(settings)
    best = get_research_current_best_envelope(settings)
    api_budget = get_research_api_budget_envelope(settings)
    moonshot = get_research_moonshot_progress_envelope(settings)
    paper = get_research_paper_forward_envelope(settings)

    assert scoreboard.data["summary"]["accepted_count"] == 1
    assert scoreboard.data["summary"]["best_proposal_id"] == "ar_candidate_1"
    assert runs.data["items"][0]["run_id"] == "ar_run_1"
    assert best.data["proposal_id"] == "ar_candidate_1"
    assert best.data["candidate_policy"]["live_allowed"] is False
    assert api_budget.data["summary"]["api_429_count"] == 1
    assert moonshot.data["summary"]["moonshot_candidates_seen"] == 9
    assert paper.data["latest"]["status"] == "accepted_paper"
    assert paper.data["status_counts"]["accepted_paper"] == 1


def test_research_api_services_handle_empty_workspace(tmp_path: Path) -> None:
    settings = _tmp_settings(tmp_path)

    assert get_research_scoreboard_envelope(settings).data["count"] == 0
    assert get_research_runs_envelope(settings).data["items"] == []
    assert get_research_current_best_envelope(settings).data["source"] == "none"
    assert get_research_api_budget_envelope(settings).data["summary"]["status"] == "ok"
    assert get_research_moonshot_progress_envelope(settings).meta.empty is True
    assert get_research_paper_forward_envelope(settings).data["items"] == []


def test_research_router_registers_read_only_get_endpoints() -> None:
    app = create_app()
    route_methods = {
        route.path: route.methods
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/v1/research/")
    }

    expected = {
        "/api/v1/research/scoreboard",
        "/api/v1/research/runs",
        "/api/v1/research/current-best",
        "/api/v1/research/api-budget",
        "/api/v1/research/moonshot-progress",
        "/api/v1/research/paper-forward",
    }
    assert expected <= set(route_methods)
    assert all(methods == {"GET"} for path, methods in route_methods.items() if path in expected)

    alias_paths = {
        route.path: route.methods
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/research/")
    }
    assert {
        "/api/research/scoreboard",
        "/api/research/runs",
        "/api/research/current-best",
        "/api/research/api-budget",
        "/api/research/moonshot-progress",
        "/api/research/paper-forward",
    } <= set(alias_paths)
    assert all(methods == {"GET"} for methods in alias_paths.values())
