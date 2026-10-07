from __future__ import annotations

import json
import pytest

from research_loop.evaluator import STATUS_NEEDS_PAPER
from research_loop.paper_forward import (
    STATUS_ACCEPTED_PAPER,
    STATUS_PAPER_FORWARD_STARTED,
    STATUS_REJECTED_PAPER,
    finalize_paper_forward,
    start_paper_forward,
)
from research_loop.policy_promoter import promote_to_paper_candidate
from research_loop.scoreboard import load_scoreboard


def _candidate(proposal_id: str = "ar_paper_001", changes: dict | None = None) -> dict:
    return {
        "proposal_id": proposal_id,
        "created_at_utc": "2026-06-04T00:00:00+00:00",
        "experiment_type": "replay",
        "hypothesis": "Paper forward accepted replay candidate",
        "target_lanes": ["pump_early_moonshot_micro_lottery"],
        "changes": changes or {"MOONSHOT_MICRO_CONFIRMATION_PNL": "75"},
        "expected_effect": {"increase_pnl": True, "increase_moonshot_capture": True},
        "optimized_metric": "total_pnl_usd",
        "optimization_scope": "combined",
        "required_gates": ["replay_positive", "api_budget_ok"],
        "api_budget_sensitive": True,
        "live_allowed": False,
        "risk_notes": ["paper only"],
    }


def _source_profile(tmp_path) -> None:
    profiles = tmp_path / "config" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "paper_hotfix_0707.env").write_text(
        "DRY_RUN=1\nPAPER_SNIPER_MODE=true\nLIVE_CANARY_ENABLED=false\n",
        encoding="utf-8",
    )


def _baseline() -> dict:
    return {
        "total_pnl_usd": 10.0,
        "avg_pnl_pct": 1.0,
        "median_pnl_pct": 1.0,
        "win_rate_pct": 40.0,
        "closed_trades": 5,
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


def _paper_metrics() -> dict:
    metrics = _baseline()
    metrics.update(
        {
            "elapsed_hours": 7.0,
            "closed_trades": 6,
            "decisions": 120,
            "daily_buys": 8,
            "total_pnl_usd": 14.0,
            "median_pnl_pct": 2.0,
            "runner_capture_ratio": 0.3,
        }
    )
    return metrics


def _api_budget(**overrides) -> dict:
    payload = {
        "gecko_429_count": 0,
        "birdeye_404_count": 0,
        "birdeye_429_count": 0,
        "jupiter_rate_limit_count": 0,
        "pumpfun_disconnect_count": 0,
        "rpc_errors": 0,
        "cooldown_count": 0,
        "provider_degraded_minutes": 0,
        "estimated_requests_by_provider": {},
    }
    payload.update(overrides)
    return payload


def test_start_paper_forward_creates_state_profile_and_budget(tmp_path) -> None:
    _source_profile(tmp_path)

    result = start_paper_forward(_candidate(), root=tmp_path, run_id="paper_start")

    assert result.status == STATUS_PAPER_FORWARD_STARTED
    assert result.state_path.exists()
    assert result.budget["min_hours"] == 6
    assert result.promotion.profile_path.exists()
    assert (result.run_dir / "baseline_metrics.json").exists()
    assert (result.run_dir / "baseline_api_budget.json").exists()
    state = json.loads(result.state_path.read_text(encoding="utf-8"))
    assert state["paper_profile"] == "paper_research_candidate_ar_paper_001"
    assert state["promotion"]["status"] == STATUS_NEEDS_PAPER
    assert state["budget"]["max_daily_buys"] == 0


def test_start_paper_forward_reuses_existing_promotion_without_rebackup(tmp_path) -> None:
    _source_profile(tmp_path)
    existing = tmp_path / "config" / "profiles" / "paper_research_candidate_ar_paper_001.env"
    existing.write_text("OLD_VALUE=1\n", encoding="utf-8")
    promotion = promote_to_paper_candidate(_candidate(), root=tmp_path)

    result = start_paper_forward(
        _candidate(),
        root=tmp_path,
        run_id="paper_reuse",
        promotion=promotion,
    )

    assert result.promotion.backup_path == promotion.backup_path
    assert result.promotion.backup_path is not None
    assert "OLD_VALUE=1" in result.promotion.backup_path.read_text(encoding="utf-8")
    state = json.loads(result.state_path.read_text(encoding="utf-8"))
    assert state["promotion"]["backup_path"] == str(promotion.backup_path)


def test_finalize_paper_forward_accepts_when_budget_api_and_objective_pass(tmp_path) -> None:
    _source_profile(tmp_path)
    start = start_paper_forward(_candidate(), root=tmp_path, run_id="paper_accept")

    result = finalize_paper_forward(
        start.run_id,
        root=tmp_path,
        paper_metrics=_paper_metrics(),
        baseline_metrics=_baseline(),
        api_budget=_api_budget(),
        baseline_api_budget=_api_budget(),
    )

    assert result.status == STATUS_ACCEPTED_PAPER
    assert result.accepted is True
    assert result.automation_eligible is False
    assert "supplied_metrics_are_diagnostic_not_automatic_policy_evidence" in result.warnings
    assert result.result_path and result.result_path.exists()
    assert load_scoreboard(tmp_path)[0]["status"] == STATUS_ACCEPTED_PAPER


def test_finalize_paper_forward_rejects_api_budget_regression_and_rolls_back_state(tmp_path) -> None:
    _source_profile(tmp_path)
    start = start_paper_forward(_candidate("ar_paper_api"), root=tmp_path, run_id="paper_api_reject")

    result = finalize_paper_forward(
        start.run_id,
        root=tmp_path,
        paper_metrics=_paper_metrics(),
        baseline_metrics=_baseline(),
        api_budget=_api_budget(gecko_429_count=1),
        baseline_api_budget=_api_budget(),
    )

    assert result.status == STATUS_REJECTED_PAPER
    assert "api_budget:api_429_count_delta>0" in result.rejection_reasons
    assert result.rollback_report_path is not None
    assert result.rollback_report_path.exists()


def test_finalize_paper_forward_rejects_missing_budget_sample(tmp_path) -> None:
    _source_profile(tmp_path)
    start = start_paper_forward(_candidate("ar_paper_small"), root=tmp_path, run_id="paper_small")
    small_metrics = _paper_metrics()
    small_metrics.update({"elapsed_hours": 1.0, "closed_trades": 1, "decisions": 20})

    result = finalize_paper_forward(
        start.run_id,
        root=tmp_path,
        paper_metrics=small_metrics,
        baseline_metrics=_baseline(),
        api_budget=_api_budget(),
        baseline_api_budget=_api_budget(),
        rollback_on_reject=False,
    )

    assert result.status == STATUS_REJECTED_PAPER
    assert any(reason.startswith("paper_budget:min_hours") for reason in result.rejection_reasons)
    assert any(reason.startswith("paper_budget:min_closed_trades") for reason in result.rejection_reasons)
    assert any(reason.startswith("paper_budget:min_decisions") for reason in result.rejection_reasons)


def test_baseline_uses_existing_run_start_and_source_profile_not_new_experiment_time(tmp_path, monkeypatch):
    import research_loop.paper_forward as forward
    _source_profile(tmp_path)
    identity = {"run_id": "prior_runtime", "run_started_at": "2026-10-01T00:00:00Z"}
    captured = []
    monkeypatch.setattr(forward, "current_run_identity", lambda *args: identity)
    monkeypatch.setattr(forward, "_load_current_paper_metrics", lambda root, state: captured.append(state) or {})
    start = start_paper_forward(_candidate("baseline"), root=tmp_path, run_id="new_trial")
    assert captured[0]["runtime_run_id"] == "prior_runtime"
    assert captured[0]["started_at_utc"] == identity["run_started_at"]
    assert captured[0]["paper_profile"] == "paper_hotfix_0707"
    state = json.loads(start.state_path.read_text())
    assert state["activation_status"] == "profile_exported_not_applied"
    assert state["comparison_kind"] == "sequential_diagnostic_not_paired"


def test_existing_forward_run_is_not_overwritten(tmp_path):
    from research_loop.paper_forward import PaperForwardError
    _source_profile(tmp_path)
    start = start_paper_forward(_candidate("preserve"), root=tmp_path, run_id="same")
    original = start.state_path.read_bytes()
    with pytest.raises(PaperForwardError, match="already_initialized"):
        start_paper_forward(_candidate("other"), root=tmp_path, run_id="same")
    assert start.state_path.read_bytes() == original


@pytest.mark.parametrize("net", [0, -1, float("nan"), float("inf")])
def test_supplied_metrics_cannot_bypass_positive_finite_net_pnl(tmp_path, net):
    _source_profile(tmp_path)
    start = start_paper_forward(_candidate("invalid_net"), root=tmp_path, run_id="invalid_net")
    metrics = _paper_metrics()
    metrics["total_pnl_usd"] = net
    result = finalize_paper_forward(start.run_id, root=tmp_path, paper_metrics=metrics,
                                    baseline_metrics=_baseline(), api_budget=_api_budget(),
                                    baseline_api_budget=_api_budget(), rollback_on_reject=False)
    assert result.status == STATUS_REJECTED_PAPER
    assert "paper_forward:nonpositive_net_pnl" in result.rejection_reasons
    assert result.automation_eligible is False
