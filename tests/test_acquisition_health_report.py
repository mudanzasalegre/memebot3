from __future__ import annotations

import json
import sqlite3

from analytics.acquisition_health_report import RECOMMENDED_ACTIONS, write_acquisition_health_report
from analytics.core_report_scheduler import regenerate_core_reports
from research_loop.report_bundle import build_report_bundle


REQUIRED_FIELDS = {
    "raw_discovered",
    "strategy_decisions",
    "buys",
    "buys_per_hour",
    "shadows",
    "requeues",
    "top_blockers",
    "allowed_by_selector",
    "micro_triggers",
    "paper_exploration_eligible",
    "paper_exploration_buys",
    "rank_canary_allowed",
    "rank_canary_buys",
    "moonshot_candidates",
    "shadow_followup_triggers",
    "shadow_followup_buys",
    "api_budget_status",
    "recommended_action",
}


def _write_json(path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _write_current_summary(root, *, raw: int = 10, decisions: int = 10, buys: int = 0) -> None:
    _write_json(
        root / "data" / "metrics" / "current_run_summary.json",
        {
            "raw_discovered": raw,
            "strategy_decisions": decisions,
            "buys": buys,
        },
    )


def test_acquisition_health_empty_data_needs_more_data(tmp_path) -> None:
    report = write_acquisition_health_report(tmp_path)

    assert REQUIRED_FIELDS <= set(report)
    assert report["recommended_action"] == "needs_more_data"
    assert report["recommendation_values"] == sorted(RECOMMENDED_ACTIONS)
    assert report["api_budget_status"]["status"] == "ok"
    assert (tmp_path / "data" / "metrics" / "acquisition_health_report.json").exists()


def test_acquisition_health_recommends_shadow_followup_when_triggers_are_unbought(tmp_path) -> None:
    _write_current_summary(tmp_path)
    _write_json(tmp_path / "data" / "metrics" / "shadow_followup_micro_report.json", {"micro_triggers": 2})

    report = write_acquisition_health_report(tmp_path)

    assert report["shadow_followup_triggers"] == 2
    assert report["recommended_action"] == "open_shadow_followup"


def test_acquisition_health_recommends_rank_micro_when_rank_allowed_unbought(tmp_path) -> None:
    _write_current_summary(tmp_path)
    _write_json(
        tmp_path / "data" / "metrics" / "research_rank_current_run_report.json",
        {
            "normal_micro_seen": 2,
            "normal_micro_bought": 0,
            "priority_seen": 1,
            "priority_bought": 0,
        },
    )

    report = write_acquisition_health_report(tmp_path)

    assert report["rank_canary_allowed"] == 3
    assert report["rank_canary_buys"] == 0
    assert report["recommended_action"] == "open_rank_micro"


def test_acquisition_health_recommends_moonshot_micro_when_tail_candidates_exist(tmp_path) -> None:
    _write_current_summary(tmp_path)
    _write_json(tmp_path / "data" / "metrics" / "moonshot_micro_lottery_report.json", {"candidates_seen": 4})

    report = write_acquisition_health_report(tmp_path)

    assert report["moonshot_candidates"] == 4
    assert report["recommended_action"] == "open_moonshot_micro"


def test_acquisition_health_recommends_reduce_api_pressure_on_budget_warning(tmp_path) -> None:
    _write_jsonl(
        tmp_path / "data" / "metrics" / "runtime_events.jsonl",
        [{"event_type": "provider_error", "provider": "GeckoTerminal", "message": "429 too many requests"}],
    )

    report = write_acquisition_health_report(tmp_path)

    assert report["api_budget_status"]["status"] == "warn"
    assert report["api_budget_status"]["api_429_count"] == 1
    assert report["recommended_action"] == "reduce_api_pressure"


def test_acquisition_health_recommends_disable_losing_lane_from_autotune(tmp_path) -> None:
    _write_current_summary(tmp_path)
    _write_json(
        tmp_path / "data" / "metrics" / "current_run_autotune_state.json",
        {"actions": [{"action": "disable_severe_loss_lane"}]},
    )

    report = write_acquisition_health_report(tmp_path)

    assert report["recommended_action"] == "disable_losing_lane"


def test_acquisition_health_keeps_current_when_flow_has_buys(tmp_path) -> None:
    _write_current_summary(tmp_path, buys=1)

    report = write_acquisition_health_report(tmp_path)

    assert report["buys"] == 1
    assert report["recommended_action"] == "keep_current"


def test_acquisition_health_dedupes_actual_buys_and_positions(tmp_path) -> None:
    _write_jsonl(
        tmp_path / "data" / "metrics" / "runtime_events.jsonl",
        [
            {
                "event_type": "actual_paper_buy",
                "address": "DUP",
                "run_id": "run-dedupe",
                "run_started_at": "2026-05-22T10:00:00+00:00",
                "ts_utc": "2026-05-22T10:01:00+00:00",
                "entry_lane": "pump_early_moonshot_micro_lottery",
            },
            {
                "event_type": "buy",
                "address": "DUP",
                "run_id": "run-dedupe",
                "run_started_at": "2026-05-22T10:00:00+00:00",
                "ts_utc": "2026-05-22T10:01:00+00:00",
                "entry_lane": "pump_early_moonshot_micro_lottery",
            },
        ],
    )
    data_dir = tmp_path / "data"
    _write_json(
        data_dir / "paper_portfolio.json",
        {
            "positions": [
                {
                    "address": "DUP",
                    "run_id": "run-dedupe",
                    "opened_at": "2026-05-22T10:01:00+00:00",
                    "entry_lane": "pump_early_moonshot_micro_lottery",
                }
            ]
        },
    )
    conn = sqlite3.connect(data_dir / "memebotdatabase.db")
    conn.execute("create table positions (address text, run_id text, opened_at text, entry_lane text)")
    conn.execute(
        "insert into positions values (?, ?, ?, ?)",
        ("DUP", "run-dedupe", "2026-05-22T10:01:00+00:00", "pump_early_moonshot_micro_lottery"),
    )
    conn.commit()
    conn.close()

    report = write_acquisition_health_report(tmp_path)

    assert report["buys"] == 1
    assert report["actual_paper_buys"] == 1
    assert report["moonshot_buys"] == 1
    assert report["source_rows"]["positions"] == 1


def test_core_scheduler_generates_acquisition_health_report(tmp_path) -> None:
    regenerate_core_reports(tmp_path)

    payload = json.loads((tmp_path / "data" / "metrics" / "acquisition_health_report.json").read_text())

    assert REQUIRED_FIELDS <= set(payload)
    assert payload["recommended_action"] in RECOMMENDED_ACTIONS


def test_report_bundle_exposes_acquisition_health_context(tmp_path) -> None:
    _write_json(
        tmp_path / "data" / "metrics" / "acquisition_health_report.json",
        {
            "recommended_action": "open_rank_micro",
            "buys": 0,
            "buys_per_hour": 0.0,
            "top_blockers": {"rank_canary_allowed_not_bought": 3},
        },
    )

    bundle = build_report_bundle(tmp_path, include_api_budget=False)

    assert bundle["acquisition"]["health"]["recommended_action"] == "open_rank_micro"
    assert bundle["recommendation_context"]["acquisition_recommended_action"] == "open_rank_micro"
    assert bundle["recommendation_context"]["acquisition_buys"] == 0
