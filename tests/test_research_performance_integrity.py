from __future__ import annotations

import json

import pytest

from research_loop.objectives import calculate_objective_score
from research_loop.replay_runner import build_replay_metric_views
from runtime.policy_tuner import generate_candidate_profiles


def _write(tmp_path, name, payload):
    (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")


def test_offline_hypotheses_do_not_invent_candidate_performance(tmp_path):
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    _write(metrics, "policy_replay.json", {"combined_policy_v2": {"total_pnl": 1e9}})
    candidates = generate_candidate_profiles(tmp_path)
    assert len(candidates) == 18
    assert len({row["proposal_id"] for row in candidates}) == 18
    assert all(row["expected_metrics"]["score"] is None for row in candidates)
    assert all(row["evaluation_status"] == "not_evaluated" for row in candidates)
    assert all(row["live_allowed"] is False for row in candidates)


def test_percentage_points_are_never_usd(tmp_path):
    _write(tmp_path, "policy_replay.json", {"current": {"total_pnl": 1000, "trades": 3}})
    _write(tmp_path, "trade_diagnostics.json", {"summary": {"total_pnl_points": 1000}})
    _write(tmp_path, "current_run_trade_diagnostics.json", {
        "current_run": {"run_id": "new"},
        "real_paper_positions": {"total_pnl_pct_points": 1000, "rows": 3},
    })
    _write(tmp_path, "current_run_summary.json", {"run_id": "new"})
    views = build_replay_metric_views(tmp_path)
    assert all(view["total_pnl_usd"] is None for view in views.values())
    assert views["historical_metrics"]["diagnostic_policy_pnl_pct_points"] == 1000
    assert views["current_run_metrics"]["closed_trades"] == 0


def test_current_run_does_not_borrow_global_profit_or_decision_pnl(tmp_path):
    _write(tmp_path, "paper_real_outcomes.json", {"summary": {
        "net_total_pnl_usd": 500, "total_pnl_usd": 600, "closed": 50,
        "avg_pnl_pct": 200, "median_pnl_pct": 150, "win_rate_pct": 100,
    }})
    _write(tmp_path, "current_run_summary.json", {"run_id": "new", "closed_trades": 0})
    _write(tmp_path, "current_run_trade_diagnostics.json", {
        "current_run": {"run_id": "new"},
        "candidate_decisions": {"avg_pnl_pct": 1000, "median_pnl_pct": 900, "win_rate_pct": 100},
    })
    metrics = build_replay_metric_views(tmp_path)["current_run_metrics"]
    assert metrics["total_pnl_usd"] is None
    assert metrics["closed_trades"] == 0
    assert metrics["avg_pnl_pct"] == 0
    assert metrics["median_pnl_pct"] == 0


@pytest.mark.parametrize("health_run", ["older", "", "new"])
def test_health_net_fallback_requires_current_run_identity(tmp_path, health_run):
    _write(tmp_path, "current_run_summary.json", {"run_id": "new", "total_pnl_usd": 999})
    _write(tmp_path, "bot_profitability_health.json", {
        "current_run": {"run_id": health_run},
        "current_run_net_total_usd": 2.5, "current_run_total_usd": 999,
    })
    metrics = build_replay_metric_views(tmp_path)["current_run_metrics"]
    assert metrics["total_pnl_usd"] == (2.5 if health_run == "new" else None)


@pytest.mark.parametrize("invalid", [None, True, "bad", float("nan"), float("inf"), -float("inf")])
def test_invalid_monetary_value_does_not_become_zero_or_profit(tmp_path, invalid):
    _write(tmp_path, "paper_real_outcomes.json", {"summary": {"net_total_pnl_usd": invalid, "total_pnl_usd": 100}})
    metrics = build_replay_metric_views(tmp_path)["historical_metrics"]
    assert metrics["total_pnl_usd"] is None


@pytest.mark.parametrize("missing", ["total_pnl_usd", "median_pnl_pct", "severe_loss_count", "api_429_count"])
def test_missing_hard_gate_metric_rejects_even_positive_capture(missing):
    base = {"total_pnl_usd": 10, "median_pnl_pct": 2, "severe_loss_count": 0, "api_429_count": 0}
    candidate = {**base, "total_pnl_usd": 20, "runner_capture_ratio": 1}
    candidate.pop(missing)
    base["runner_capture_ratio"] = 0
    config = {
        "objective": {"runner_capture_ratio_weight": 100},
        "hard_gates": {f"{key}_delta_min" if key in {"total_pnl_usd", "median_pnl_pct"} else f"{key}_delta_max": 0 for key in base if key != "runner_capture_ratio"},
    }
    result = calculate_objective_score(base, candidate, config)
    assert result.score > 0
    assert not result.accepted
    assert f"missing_hard_gate_metric:{missing}" in result.rejection_reasons


def test_unknown_api_sources_are_not_imputed_zero():
    base = {"total_pnl_usd": 1, "gecko_429_count": 1}
    candidate = {"total_pnl_usd": 2, "gecko_429_count": 2}
    config = {"objective": {"total_pnl_usd_weight": 1}, "hard_gates": {"api_429_count_delta_max": 0}}
    result = calculate_objective_score(base, candidate, config)
    assert not result.accepted
    assert "api_429_count" not in result.metric_deltas


def test_complete_zero_api_sources_are_valid_observations():
    base = {"total_pnl_usd": 1, "gecko_429_count": 0, "birdeye_429_count": 0, "jupiter_rate_limit_count": 0}
    candidate = {**base, "total_pnl_usd": 2}
    config = {"objective": {"total_pnl_usd_weight": 1}, "hard_gates": {"api_429_count_delta_max": 0}}
    result = calculate_objective_score(base, candidate, config)
    assert result.accepted
    assert result.metric_deltas["api_429_count"] == 0


@pytest.mark.parametrize("part", ["objective", "hard_gates", "penalties"])
def test_nonfinite_objective_configuration_rejects(part):
    config = {"objective": {"total_pnl_usd_weight": 1}, "hard_gates": {"total_pnl_usd_delta_min": 0}, "penalties": {"severe_loss_penalty": 1}}
    key = next(iter(config[part]))
    config[part][key] = float("nan")
    result = calculate_objective_score({"total_pnl_usd": 1, "severe_loss_count": 0}, {"total_pnl_usd": 2, "severe_loss_count": 1}, config)
    assert not result.accepted
