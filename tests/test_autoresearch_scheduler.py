from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from research_loop.scheduler import (
    IDLE_FOCUS_SPACES,
    AutoResearchSchedulerError,
    detect_idle_trigger,
    evaluate_paper_profitability_for_demotion,
    load_scheduler_config,
    run_autoresearch_cycle,
    select_research_spaces,
)
from research_loop.paper_forward import start_paper_forward


def _candidate(proposal_id: str = "ar_scheduler_001", changes: dict | None = None) -> dict:
    return {
        "proposal_id": proposal_id,
        "created_at_utc": "2026-06-04T00:00:00+00:00",
        "experiment_type": "replay",
        "hypothesis": "Scheduler test candidate",
        "target_lanes": ["pump_early_moonshot_micro_lottery"],
        "changes": changes or {"MOONSHOT_MICRO_CONFIRMATION_PNL": "75"},
        "expected_effect": {"increase_pnl": True, "increase_moonshot_capture": True},
        "required_gates": ["replay_positive", "api_budget_ok"],
        "api_budget_sensitive": True,
        "live_allowed": False,
        "risk_notes": ["paper only"],
    }


def _source_profile(tmp_path: Path) -> None:
    profiles = tmp_path / "config" / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "paper_hotfix_runner_v2.env").write_text(
        "DRY_RUN=1\nPAPER_SNIPER_MODE=true\nLIVE_CANARY_ENABLED=false\n",
        encoding="utf-8",
    )


def _bundle(idle_hours: float = 0.0) -> dict:
    return {
        "current_run": {
            "summary": {
                "idle_no_buy_hours": idle_hours,
                "strategy_decisions": 0,
                "buys": 0,
            }
        },
        "recommendation_context": {},
    }


def _baseline() -> dict:
    return {
        "total_pnl_usd": 10.0,
        "avg_pnl_pct": 2.0,
        "median_pnl_pct": 2.0,
        "win_rate_pct": 50.0,
        "closed_trades": 10,
        "runner_capture_ratio": 0.2,
        "moonshot_peak100_capture": 0.0,
        "moonshot_peak500_capture": 0.0,
        "moonshot_peak1000_capture": 0.0,
        "severe_loss_count": 0,
        "liquidity_crush_count": 0,
        "adverse_tick_count": 0,
        "no_pump_exit_count": 0,
        "max_drawdown_proxy": 0.0,
        "api_429_count": 0,
        "provider_degraded_minutes": 0,
        "overtrading_count": 0,
        "idle_no_buy_hours": 0,
    }


@dataclass(frozen=True)
class _FakeBatchResult:
    proposal_id: str
    run_id: str
    status: str
    objective_score: float


@dataclass(frozen=True)
class _FakeBatch:
    batch_id: str
    space: str
    results: list[_FakeBatchResult]

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "space": self.space,
            "results": [result.__dict__ for result in self.results],
        }


def test_idle_trigger_focuses_paper_micro_spaces() -> None:
    config = load_scheduler_config(overrides={"max_parallel": 3, "idle_threshold_hours": 2})
    idle = detect_idle_trigger(_bundle(idle_hours=4.0), idle_threshold_hours=2.0)
    selection = select_research_spaces(_bundle(idle_hours=4.0), [], config=config, seed=1)

    assert idle.active is True
    assert selection.mode == "idle_focus"
    assert selection.spaces == list(IDLE_FOCUS_SPACES)
    assert all("live" not in space for space in selection.spaces)


def test_scheduler_rejects_live_promotion_config() -> None:
    with pytest.raises(AutoResearchSchedulerError, match="AUTORESEARCH_AUTO_LIVE_PROMOTE_must_be_false"):
        load_scheduler_config(overrides={"auto_live_promote": True})

    with pytest.raises(AutoResearchSchedulerError, match="AUTORESEARCH_LIVE_PROMOTION_ENABLED_must_be_false"):
        load_scheduler_config(overrides={"live_promotion_enabled": True})


def test_run_cycle_builds_reports_runs_batch_and_promotes_best_accepted(tmp_path) -> None:
    _source_profile(tmp_path)

    def fake_batch_runner(**kwargs):
        run_id = "ar_scheduler_run"
        run_dir = tmp_path / "data" / "research_runs" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "candidate_policy.json").write_text(json.dumps(_candidate("ar_scheduler_promote")), encoding="utf-8")
        return _FakeBatch(
            batch_id=str(kwargs["batch_id"]),
            space=str(kwargs["space_name"]),
            results=[
                _FakeBatchResult(
                    proposal_id="ar_scheduler_promote",
                    run_id=run_id,
                    status="accepted_replay",
                    objective_score=12.0,
                )
            ],
        )

    result = run_autoresearch_cycle(
        root=tmp_path,
        config={
            "space": "moonshot_micro",
            "max_candidates_per_cycle": 1,
            "auto_paper_promote": True,
            "profitability_demotion_enabled": False,
        },
        seed=7,
        batch_runner_func=fake_batch_runner,
    )

    assert result.status == "completed"
    assert result.selected_spaces == ["moonshot_micro"]
    assert result.paper_forward_start is not None
    profile_path = Path(result.paper_forward_start["promotion"]["profile_path"])
    assert profile_path.exists()
    profile_text = profile_path.read_text(encoding="utf-8")
    assert "DRY_RUN=1" in profile_text
    assert "AUTORESEARCH_AUTO_LIVE_PROMOTE=false" in profile_text
    assert (tmp_path / "data" / "research_runs" / "logs" / "autoresearch_cycle_latest.json").exists()


def test_profitability_demotion_rolls_back_degraded_paper_profile(tmp_path) -> None:
    _source_profile(tmp_path)
    target = tmp_path / "config" / "profiles" / "paper_research_candidate_current.env"
    target.write_text("OLD_VALUE=1\n", encoding="utf-8")
    start = start_paper_forward(
        _candidate("ar_demote"),
        root=tmp_path,
        run_id="paper_demote",
        profile_id="current",
    )
    assert "MOONSHOT_MICRO_CONFIRMATION_PNL=75" in target.read_text(encoding="utf-8")

    paper_metrics = _baseline()
    paper_metrics.update(
        {
            "total_pnl_usd": 4.0,
            "median_pnl_pct": -5.0,
            "runner_capture_ratio": 0.1,
            "severe_loss_count": 1,
            "liquidity_crush_count": 1,
        }
    )
    result = evaluate_paper_profitability_for_demotion(
        root=tmp_path,
        run_id_or_dir=start.run_dir,
        baseline_metrics=_baseline(),
        paper_metrics=paper_metrics,
    )

    assert result.degraded is True
    assert result.status == "rejected_paper"
    assert result.rollback is not None
    assert result.rollback.restored is True
    assert "OLD_VALUE=1" in target.read_text(encoding="utf-8")
    assert result.demotion_report_path is not None
    assert result.demotion_report_path.exists()
    state = json.loads((start.run_dir / "paper_forward_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "rejected_paper"


def test_profitability_demotion_keeps_healthy_paper(tmp_path) -> None:
    _source_profile(tmp_path)
    start = start_paper_forward(_candidate("ar_healthy"), root=tmp_path, run_id="paper_healthy", profile_id="healthy")
    paper_metrics = _baseline()
    paper_metrics.update({"total_pnl_usd": 14.0, "median_pnl_pct": 3.0, "runner_capture_ratio": 0.3})

    result = evaluate_paper_profitability_for_demotion(
        root=tmp_path,
        run_id_or_dir=start.run_dir,
        baseline_metrics=_baseline(),
        paper_metrics=paper_metrics,
    )

    assert result.status == "healthy"
    assert result.degraded is False
    assert result.rollback is None
