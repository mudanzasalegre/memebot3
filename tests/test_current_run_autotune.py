from __future__ import annotations

import datetime as dt
import json

from analytics.current_run_autotune import write_current_run_autotune_state
from research_loop.report_bundle import build_report_bundle


def _write_jsonl(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _write_portfolio(root, positions: list[dict]) -> None:
    path = root / "data" / "paper_portfolio.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"positions": positions}), encoding="utf-8")


def test_autotune_relaxes_only_micro_lanes_after_three_idle_hours(tmp_path) -> None:
    started = dt.datetime(2026, 6, 5, 8, 0, tzinfo=dt.timezone.utc)
    _write_jsonl(
        tmp_path / "data" / "metrics" / "runtime_events.jsonl",
        [
            {
                "event_type": "strategy_decision",
                "run_id": "run-idle",
                "run_started_at": started.isoformat(),
                "ts_utc": started.isoformat(),
            },
            {
                "event_type": "strategy_decision",
                "run_id": "run-idle",
                "run_started_at": started.isoformat(),
                "ts_utc": (started + dt.timedelta(hours=3, minutes=15)).isoformat(),
            },
        ],
    )

    report = write_current_run_autotune_state(tmp_path)

    assert report["current_run_id"] == "run-idle"
    assert any(action["action"] == "relax_micro_lanes" for action in report["actions"])
    assert report["recommended_changes"]["SHADOW_FOLLOWUP_TRIGGER_PNL_3M"] <= 20.0
    assert report["recommended_changes"]["RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED"] is True
    assert report["recommended_changes"]["PAPER_IDLE_AFTER_HOURS"] == 0
    assert not any("LIVE" in key for key in report["recommended_changes"])
    assert report["applied_changes"] == {}


def test_autotune_disables_paper_exploration_when_current_run_loss_exceeds_threshold(tmp_path) -> None:
    _write_portfolio(
        tmp_path,
        [
            {
                "entry_lane": "pump_early_paper_exploration_micro",
                "closed": True,
                "closed_at": "2026-06-05T11:00:00+00:00",
                "realized_pnl_pct": -8,
                "realized_pnl_usd": -8,
            }
        ],
    )

    report = write_current_run_autotune_state(tmp_path)

    assert any(action["action"] == "disable_paper_exploration" for action in report["actions"])
    assert report["recommended_changes"]["PAPER_EXPLORATION_QUOTA_ENABLED"] is False
    assert report["recommended_changes"]["PAPER_IDLE_MICRO_EXPLORATION_ENABLED"] is False


def test_autotune_rank_and_moonshot_three_loss_streaks(tmp_path) -> None:
    positions = []
    for lane in ("pump_early_research_rank_canary", "pump_early_moonshot_micro_lottery"):
        for idx in range(3):
            positions.append(
                {
                    "entry_lane": lane,
                    "closed": True,
                    "closed_at": f"2026-06-05T11:0{idx}:00+00:00",
                    "realized_pnl_pct": -5,
                    "realized_pnl_usd": -0.1,
                }
            )
    _write_portfolio(tmp_path, positions)

    report = write_current_run_autotune_state(tmp_path)

    assert any(action["action"] == "rank_canary_shadow_only" for action in report["actions"])
    assert any(action["action"] == "review_moonshot_loss_streak" for action in report["actions"])
    assert report["recommended_changes"]["RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED"] is False
    assert "MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS" not in report["recommended_changes"]


def test_autotune_disables_lane_when_severe_loss_count_increases(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "current_run_autotune_state.json").write_text(
        json.dumps({"snapshot": {"severe_loss_count": 0}}),
        encoding="utf-8",
    )
    _write_portfolio(
        tmp_path,
        [
            {
                "entry_lane": "pump_early_shadow_followup_micro",
                "closed": True,
                "closed_at": "2026-06-05T11:00:00+00:00",
                "realized_pnl_pct": -30,
                "realized_pnl_usd": -1,
            }
        ],
    )

    report = write_current_run_autotune_state(tmp_path)

    assert any(action["action"] == "disable_severe_loss_lane" for action in report["actions"])
    assert report["recommended_changes"]["SHADOW_FOLLOWUP_MICRO_ENABLED"] is False


def test_autotune_guards_overactive_low_quality_bootstrap(tmp_path) -> None:
    started = dt.datetime(2026, 6, 5, 8, 0, tzinfo=dt.timezone.utc)
    _write_jsonl(
        tmp_path / "data" / "metrics" / "runtime_events.jsonl",
        [
            {
                "event_type": "strategy_decision",
                "run_id": "run-bootstrap",
                "run_started_at": started.isoformat(),
                "ts_utc": started.isoformat(),
            }
        ],
    )
    _write_portfolio(
        tmp_path,
        [
            {
                "entry_lane": "pump_early_paper_bootstrap_micro",
                "closed": True,
                "closed_at": (started + dt.timedelta(minutes=idx)).isoformat(),
                "realized_pnl_pct": -1,
                "realized_pnl_usd": -0.01,
            }
            for idx in range(25)
        ],
    )

    report = write_current_run_autotune_state(tmp_path)

    assert any(action["action"] == "bootstrap_quality_guard" for action in report["actions"])
    assert "PAPER_BOOTSTRAP_REQUIRE_COLD_START" not in report["recommended_changes"]
    assert report["recommended_changes"]["PAPER_BOOTSTRAP_REQUIRE_REAL_LIQUIDITY"] is True
    assert report["recommended_changes"]["PAPER_BOOTSTRAP_REQUIRE_ROUTE"] is True
    assert report["recommended_changes"]["PAPER_BOOTSTRAP_AMOUNT_SOL"] == 0.1
    assert report["recommended_changes"]["PAPER_BOOTSTRAP_MAX_AMOUNT_SOL"] == 0.1


def test_autotune_keeps_winning_shadow_followup_micro(tmp_path) -> None:
    _write_portfolio(
        tmp_path,
        [
            {
                "entry_lane": "pump_early_shadow_followup_micro",
                "closed": True,
                "closed_at": "2026-06-05T11:00:00+00:00",
                "realized_pnl_pct": 22,
                "realized_pnl_usd": 1,
            }
        ],
    )

    report = write_current_run_autotune_state(tmp_path)

    assert any(action["action"] == "keep_shadow_followup_micro" for action in report["actions"])
    assert report["lane_states"]["shadow_followup_micro"]["last_result"] == "win"


def test_report_bundle_includes_current_run_autotune(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "current_run_autotune_state.json").write_text(
        json.dumps({"actions": [{"action": "relax_micro_lanes"}], "recommended_changes": {"A": 1}}),
        encoding="utf-8",
    )

    bundle = build_report_bundle(tmp_path, include_api_budget=False)

    assert bundle["current_run"]["autotune"]["actions"][0]["action"] == "relax_micro_lanes"
    assert bundle["recommendation_context"]["current_run_autotune_actions"] == 1
