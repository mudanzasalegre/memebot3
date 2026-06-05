from __future__ import annotations

import json
from pathlib import Path

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
        "required_gates": ["replay_positive", "api_budget_ok"],
        "api_budget_sensitive": True,
        "live_allowed": False,
        "risk_notes": ["paper only"],
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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
    assert result.replay_metrics["total_pnl_usd"] == 12.0
    assert result.replay_metrics["closed_trades"] == 7
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
