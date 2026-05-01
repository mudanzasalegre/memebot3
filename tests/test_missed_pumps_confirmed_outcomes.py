from __future__ import annotations

import json

from analytics.missed_pumps import build_missed_pumps


def _write_candidate(tmp_path, row: dict) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (metrics / "runtime_events.jsonl").write_text("", encoding="utf-8")


def test_hot_seen_with_unconfirmed_target_is_not_confirmed_winner(tmp_path) -> None:
    _write_candidate(tmp_path, {"address": "HOT", "price_pct_5m": 120, "target_total_pnl_pct": 400})
    report = build_missed_pumps(tmp_path)
    assert report[0]["classification"] == "hot_seen_not_bought"
    assert report[0]["confirmed_later_peak_pct"] is None


def test_confirmed_sample_type_can_mark_missed_winner(tmp_path) -> None:
    _write_candidate(tmp_path, {"address": "WIN", "price_pct_5m": 20, "target_total_pnl_pct": 150, "sample_type": "shadow_close"})
    assert build_missed_pumps(tmp_path)[0]["classification"] == "confirmed_missed_winner"


def test_confirmed_avoided_loser_requires_confirmed_outcome(tmp_path) -> None:
    _write_candidate(tmp_path, {"address": "LOSE", "price_pct_5m": 10, "pnl_pct": -40})
    assert build_missed_pumps(tmp_path)[0]["classification"] == "unresolved_hot_candidate"


def test_outcome_confirmed_flag_is_enough(tmp_path) -> None:
    _write_candidate(tmp_path, {"address": "LOSE2", "price_pct_5m": 10, "pnl_pct": -40, "outcome_confirmed": True})
    assert build_missed_pumps(tmp_path)[0]["classification"] == "confirmed_avoided_loser"
