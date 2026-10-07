from __future__ import annotations

import json
from pathlib import Path

from backtest.policy_replay import build_policy_replay
from research_loop.evaluator import evaluate_replay_candidate
from research_loop.replay_runner import REPLAY_REPORTS, run_research_replay


def _candidate() -> dict:
    return {
        "proposal_id": "ar_20260604_002",
        "created_at_utc": "2026-06-04T00:00:00+00:00",
        "experiment_type": "replay",
        "hypothesis": "Tune rank canary threshold",
        "target_lanes": ["pump_early_sniper_research"],
        "changes": {"RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE": "72"},
        "expected_effect": {"increase_win_rate": True},
        "optimized_metric": "total_pnl_usd",
        "optimization_scope": "combined",
        "required_gates": ["replay_positive", "api_budget_ok"],
        "api_budget_sensitive": True,
        "live_allowed": False,
        "risk_notes": ["paper only"],
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_replay_runner_writes_metrics_and_snapshot(tmp_path) -> None:
    def fake_regenerate(root: Path) -> dict:
        metrics = root / "data" / "metrics"
        for name in REPLAY_REPORTS:
            _write_json(metrics / name, {"generated_at_utc": "2026-06-04T00:00:00+00:00"})
        _write_json(
            metrics / "policy_replay.json",
            {
                "current": {
                    "total_pnl": 12.0,
                    "avg_pnl": 3.0,
                    "median_pnl": 2.0,
                    "win_rate": 55.0,
                    "trades": 7,
                    "runner_capture_ratio": 0.25,
                    "severe_loss_count": 0,
                    "liq_crush_count": 0,
                    "adverse_tick_count": 0,
                    "max_drawdown_proxy": 0.0,
                }
            },
        )
        _write_json(
            metrics / "trade_diagnostics.json",
            {
                "summary": {"trades": 7},
                "groups": {
                    "exit_reason:STOP_LOSS": {"trades": 0},
                    "exit_reason:NO_PUMP_EXIT": {"trades": 1},
                },
            },
        )
        _write_json(metrics / "bot_profitability_health.json", {"buys_per_hour": 1.5})
        _write_json(metrics / "runner_capture_ladder_report.json", {"summary": {"avg_current_capture_ratio": 0.3}})
        _write_json(
            metrics / "moonshot_micro_lottery_report.json",
            {"peak100_captured": 1, "peak500_captured": 0, "peak1000_captured": 0, "tail_capture_ratio": 0.5},
        )
        return {"warnings": {}}

    result = run_research_replay(_candidate(), root=tmp_path, run_id="ar_replay", regenerate_func=fake_regenerate)

    assert result.status == "completed"
    assert result.replay_metrics["total_pnl_usd"] is None
    assert result.replay_metrics["diagnostic_policy_pnl_pct_points"] == 12.0
    assert result.replay_metrics["closed_trades"] == 7
    assert result.replay_metrics["combined_metrics"]["total_pnl_usd"] is None
    assert result.replay_metrics["historical_metrics"]["closed_trades"] == 7
    assert "current_run_metrics" in result.replay_metrics
    assert result.replay_metrics["overtrading_count"] == 0
    assert result.replay_metrics["idle_no_buy_hours"] == 0.0
    assert result.replay_metrics_path.exists()
    assert (result.report_snapshot_dir / "policy_replay.json").exists()
    assert (result.run_dir / "candidate_diff.md").exists()


def test_replay_runner_marks_failed_when_reports_missing(tmp_path) -> None:
    def fake_regenerate(root: Path) -> dict:
        metrics = root / "data" / "metrics"
        _write_json(metrics / "policy_replay.json", {"current": {"total_pnl": 0}})
        return {"warnings": {}}

    result = run_research_replay(_candidate(), root=tmp_path, run_id="ar_replay_missing", regenerate_func=fake_regenerate)

    assert result.status == "failed"
    assert result.replay_metrics["failed"] is True
    assert result.failures


def test_candidate_lowering_shadow_followup_threshold_increases_allowed_count(tmp_path) -> None:
    _write_jsonl(
        tmp_path / "data" / "metrics" / "candidate_outcomes.jsonl",
        [
            {
                "address": "LOWER_ONLY",
                "sample_type": "shadow",
                "shadow_pnl_pct": 22,
                "minutes_since_first_seen": 2,
                "market_cap_usd": 80_000,
                "has_jupiter_route": True,
                "target_total_pnl_pct": 30,
            },
            {
                "address": "DEFAULT_OK",
                "sample_type": "shadow",
                "shadow_pnl_pct": 28,
                "minutes_since_first_seen": 2,
                "market_cap_usd": 80_000,
                "has_jupiter_route": True,
                "target_total_pnl_pct": 12,
            },
            {
                "address": "SIX_MINUTE",
                "sample_type": "shadow",
                "shadow_pnl_pct": 45,
                "minutes_since_first_seen": 5,
                "market_cap_usd": 80_000,
                "has_jupiter_route": True,
                "target_total_pnl_pct": 20,
            },
        ],
    )

    lower = build_policy_replay(
        tmp_path,
        candidate_config={
            "SHADOW_FOLLOWUP_TRIGGER_PNL_3M": "20",
            "SHADOW_FOLLOWUP_TRIGGER_PNL_6M": "40",
            "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL": "0.003",
        },
    )
    higher = build_policy_replay(
        tmp_path,
        candidate_config={
            "SHADOW_FOLLOWUP_TRIGGER_PNL_3M": "35",
            "SHADOW_FOLLOWUP_TRIGGER_PNL_6M": "60",
            "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL": "0.003",
        },
    )

    assert lower["current"]["allowed_shadow_followup"] == 3
    assert lower["current"]["simulated_buys"] == 3
    assert lower["current"]["objective_score"] > higher["current"]["objective_score"]
    assert higher["current"]["allowed_shadow_followup"] == 0
    assert higher["current"]["simulated_buys"] == 0


def test_unused_candidate_change_warns_no_effect_detected() -> None:
    candidate = _candidate()
    candidate["changes"] = {"UNUSED_REPLAY_SETTING": "1"}
    baseline = {
        "total_pnl_usd": 1.0,
        "median_pnl_pct": 1.0,
        "runner_capture_ratio": 0.2,
    }
    result = evaluate_replay_candidate(candidate, baseline, dict(baseline))

    assert "no_effect_detected" in result.warnings


def test_candidate_env_does_not_modify_real_env_file(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DRY_RUN=1\nSHADOW_FOLLOWUP_TRIGGER_PNL_3M=25\n", encoding="utf-8")

    def fake_regenerate(root: Path) -> dict:
        metrics = root / "data" / "metrics"
        for name in REPLAY_REPORTS:
            _write_json(metrics / name, {"generated_at_utc": "2026-06-04T00:00:00+00:00"})
        _write_json(
            metrics / "policy_replay.json",
            {
                "current": {
                    "total_pnl": 0,
                    "avg_pnl": 0,
                    "median_pnl": 0,
                    "win_rate": 0,
                    "trades": 0,
                    "runner_capture_ratio": 0,
                }
            },
        )
        return {"warnings": {}}

    candidate = _candidate()
    candidate["changes"] = {"SHADOW_FOLLOWUP_TRIGGER_PNL_3M": "20"}
    result = run_research_replay(candidate, root=tmp_path, run_id="ar_env_isolated", regenerate_func=fake_regenerate)

    assert result.status == "completed"
    assert env_path.read_text(encoding="utf-8") == "DRY_RUN=1\nSHADOW_FOLLOWUP_TRIGGER_PNL_3M=25\n"
    assert "SHADOW_FOLLOWUP_TRIGGER_PNL_3M=20" in (result.run_dir / "candidate.env").read_text(encoding="utf-8")


def test_replay_runner_uses_event_replay_metrics_for_acceptance(tmp_path) -> None:
    _write_jsonl(
        tmp_path / "data" / "metrics" / "candidate_outcomes.jsonl",
        [
            {
                "address": "EVENT_ACCEPT",
                "source": "pumpfun",
                "first_seen_at": "2026-07-07T10:00:00+00:00",
                "closed_at": "2026-07-07T10:05:00+00:00",
                "age_minutes": 2,
                "price_pct_5m": 650,
                "txns_last_5m": 320,
                "market_cap_usd": 80_000,
                "has_jupiter_route": True,
                "entry_notional_usd": 10.0,
                "actual_buy_amount_sol": 0.001,
                "pnl_pct": 20,
                "max_pnl_pct": 25,
                "sample_type": "shadow_close",
            }
        ],
    )

    def fake_regenerate(root: Path) -> dict:
        metrics = root / "data" / "metrics"
        for name in REPLAY_REPORTS:
            _write_json(metrics / name, {"generated_at_utc": "2026-06-04T00:00:00+00:00"})
        _write_json(
            metrics / "policy_replay.json",
            {
                "current": {
                    "total_pnl": 999.0,
                    "avg_pnl": 999.0,
                    "median_pnl": 999.0,
                    "win_rate": 100.0,
                    "trades": 99,
                    "runner_capture_ratio": 1.0,
                    "severe_loss_count": 0,
                    "max_drawdown_proxy": 0.0,
                }
            },
        )
        return {"warnings": {}}

    candidate = _candidate()
    candidate["changes"] = {"SHADOW_FOLLOWUP_TRIGGER_PNL_3M": "20"}

    result = run_research_replay(candidate, root=tmp_path, run_id="ar_event_acceptance", regenerate_func=fake_regenerate)

    assert result.status == "completed"
    assert result.replay_metrics["event_replay_used_for_acceptance"] is True
    assert result.replay_metrics["closed_trades"] == 1
    assert result.replay_metrics["total_pnl_usd"] != 999.0
    assert result.replay_metrics["diagnostic_policy_replay_total_pnl_usd"] is None
    assert result.replay_metrics["diagnostic_policy_pnl_pct_points"] == 999.0
    assert (result.report_snapshot_dir / "event_replay.json").exists()


def test_replay_runner_does_not_overwrite_shared_event_replay_or_api_budget(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    for name in REPLAY_REPORTS:
        _write_json(metrics / name, {"sentinel": name})
    _write_jsonl(
        metrics / "candidate_outcomes.jsonl",
        [
            {
                "address": "ISOLATED",
                "source": "pumpfun",
                "first_seen_at": "2026-07-07T10:00:00+00:00",
                "closed_at": "2026-07-07T10:05:00+00:00",
                "age_minutes": 2,
                "price_pct_5m": 650,
                "txns_last_5m": 320,
                "market_cap_usd": 80_000,
                "has_jupiter_route": True,
                "pnl_pct": 20,
                "sample_type": "shadow_close",
            }
        ],
    )
    shared_event = metrics / "event_replay.json"
    shared_api_budget = metrics / "api_budget_report.json"
    before = shared_event.read_text(encoding="utf-8")

    result = run_research_replay(_candidate(), root=tmp_path, run_id="ar_no_shared_writes", regenerate=False)

    assert result.status == "completed"
    assert shared_event.read_text(encoding="utf-8") == before
    assert not shared_api_budget.exists()
    assert (result.report_snapshot_dir / "event_replay.json").exists()
