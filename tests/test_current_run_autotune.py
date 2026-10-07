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
    assert any(
        block["lane"] == "pump_early_paper_exploration_micro"
        for block in report["runtime_overlay"]["blocked_lanes"]
    )
    assert report["runtime_overlay"]["live_guarded"] is True


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
    assert any(
        block["lane"] == "pump_early_shadow_followup_micro"
        for block in report["runtime_overlay"]["blocked_lanes"]
    )


def test_autotune_runtime_overlay_cools_down_no_pump_loss_streak(tmp_path) -> None:
    _write_portfolio(
        tmp_path,
        [
            {
                "entry_lane": "pump_early_moonshot_micro_lottery",
                "closed": True,
                "closed_at": f"2026-06-05T11:0{idx}:00+00:00",
                "exit_reason": "NO_PUMP_EXIT",
                "realized_pnl_pct": -4,
                "realized_pnl_usd": -0.1,
            }
            for idx in range(3)
        ],
    )

    report = write_current_run_autotune_state(tmp_path)

    assert any(action["action"] == "cooldown_toxic_exit_lane" for action in report["actions"])
    assert report["recommended_changes"]["MOONSHOT_MICRO_LOTTERY_ENABLED"] is False
    assert any(
        block["lane"] == "pump_early_moonshot_micro_lottery"
        and block["action"] == "cooldown_toxic_exit_lane"
        for block in report["runtime_overlay"]["blocked_lanes"]
    )


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
                    "run_id": "run-bootstrap",
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


def test_autotune_persistently_blocks_negative_expectancy_lane_even_after_last_win(tmp_path) -> None:
    positions = []
    for idx in range(10):
        pnl = 5.0 if idx == 9 else -30.0
        positions.append(
            {
                "entry_lane": "pump_early_shadow_followup_micro",
                "closed": True,
                "closed_at": f"2026-06-05T11:{idx:02}:00+00:00",
                "total_pnl_pct": pnl,
                "total_pnl_usd": 0.05 if pnl > 0 else -0.2,
                "exit_reason": "DYNAMIC_RUNNER_FLOOR" if pnl > 0 else "MAX_ADVERSE_EXCURSION",
            }
        )
    _write_portfolio(tmp_path, positions)

    report = write_current_run_autotune_state(tmp_path)

    assert report["lane_states"]["shadow_followup_micro"]["severe_loss_count"] == 9
    assert any(action["action"] == "disable_negative_expectancy_lane" for action in report["actions"])
    assert not any(action["action"] == "keep_shadow_followup_micro" for action in report["actions"])
    assert report["recommended_changes"]["SHADOW_FOLLOWUP_MICRO_ENABLED"] is False
    assert any(
        block["lane"] == "pump_early_shadow_followup_micro"
        and block["action"] == "disable_negative_expectancy_lane"
        for block in report["runtime_overlay"]["blocked_lanes"]
    )


def test_autotune_carries_active_lane_cooldown_across_refreshes(tmp_path) -> None:
    _write_portfolio(
        tmp_path,
        [
            {
                "entry_lane": "pump_early_shadow_followup_micro",
                "closed": True,
                "closed_at": "2026-06-05T11:00:00+00:00",
                "total_pnl_pct": -30,
                "total_pnl_usd": -0.1,
            }
        ],
    )

    first = write_current_run_autotune_state(tmp_path)
    second = write_current_run_autotune_state(tmp_path)

    assert any(action["action"] == "disable_severe_loss_lane" for action in first["actions"])
    assert any(action["action"] == "carry_forward_lane_cooldown" for action in second["actions"])
    assert any(block["lane"] == "pump_early_shadow_followup_micro" for block in second["runtime_overlay"]["blocked_lanes"])


def test_autotune_severe_delta_blocks_lane_that_changed_not_historical_max(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "current_run_autotune_state.json").write_text(
        json.dumps(
            {
                "current_run_id": "legacy",
                "lane_states": {
                    "paper_bootstrap": {"severe_loss_count": 5},
                    "shadow_followup_micro": {"severe_loss_count": 0},
                },
                "snapshot": {"severe_loss_count": 5},
            }
        ),
        encoding="utf-8",
    )
    positions = [
        {
            "entry_lane": "pump_early_paper_bootstrap_micro",
            "closed": True,
            "closed_at": f"2026-06-05T10:{idx:02}:00+00:00",
            "total_pnl_pct": -30,
            "total_pnl_usd": -0.1,
        }
        for idx in range(5)
    ]
    positions.append(
        {
            "entry_lane": "pump_early_shadow_followup_micro",
            "closed": True,
            "closed_at": "2026-06-05T11:00:00+00:00",
            "total_pnl_pct": -30,
            "total_pnl_usd": -0.1,
        }
    )
    _write_portfolio(tmp_path, positions)

    report = write_current_run_autotune_state(tmp_path)
    severe_actions = [action for action in report["actions"] if action["action"] == "disable_severe_loss_lane"]

    assert [action["lane"] for action in severe_actions] == ["shadow_followup_micro"]


def test_autotune_resets_severe_baseline_when_run_changes(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "current_run_autotune_state.json").write_text(
        json.dumps({"current_run_id": "old-run", "snapshot": {"severe_loss_count": 10}}),
        encoding="utf-8",
    )
    _write_jsonl(
        metrics / "runtime_events.jsonl",
        [
            {
                "event_type": "strategy_decision",
                "run_id": "new-run",
                "run_started_at": "2026-06-05T10:00:00+00:00",
                "ts_utc": "2026-06-05T10:00:00+00:00",
            }
        ],
    )
    _write_portfolio(
        tmp_path,
        [
            {
                "run_id": "new-run",
                "entry_lane": "pump_early_shadow_followup_micro",
                "closed": True,
                "closed_at": "2026-06-05T11:00:00+00:00",
                "total_pnl_pct": -30,
                "total_pnl_usd": -0.1,
            }
        ],
    )

    report = write_current_run_autotune_state(tmp_path)

    assert any(action["action"] == "disable_severe_loss_lane" for action in report["actions"])
    assert report["snapshot"]["previous_severe_loss_count"] == 0


def test_autotune_toxic_exit_uses_exit_reason_not_entry_reason(tmp_path) -> None:
    _write_portfolio(
        tmp_path,
        [
            {
                "entry_lane": "pump_early_shadow_followup_micro",
                "entry_reason": "shadow_followup_micro:real_liquidity_breakout",
                "exit_reason": "LIQUIDITY_CRUSH",
                "closed": True,
                "closed_at": f"2026-06-05T11:0{idx}:00+00:00",
                "total_pnl_pct": -90,
                "total_pnl_usd": -0.2,
            }
            for idx in range(3)
        ],
    )

    report = write_current_run_autotune_state(tmp_path)

    assert report["lane_states"]["shadow_followup_micro"]["toxic_exit_count"] == 3
    assert report["lane_states"]["shadow_followup_micro"]["consecutive_toxic_exits"] == 3
    assert any(action["action"] == "cooldown_toxic_exit_lane" for action in report["actions"])


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
