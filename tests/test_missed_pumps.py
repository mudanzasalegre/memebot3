from __future__ import annotations

import json

from analytics.missed_pumps import build_missed_pumps


def test_missed_pump_reports_blocking_rule(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    row = {
        "address": "A",
        "discovered_via": "pumpfun",
        "age_minutes": 1,
        "liquidity_usd": 5000,
        "market_cap_usd": 20000,
        "price_pct_5m": 120,
        "txns_last_5m": 100,
        "reason": "max_open",
        "max_pnl_pct": 350,
        "sample_type": "green_sniper_reject_shadow",
    }
    (metrics / "candidate_outcomes.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (metrics / "runtime_events.jsonl").write_text("", encoding="utf-8")
    report = build_missed_pumps(tmp_path)
    assert report[0]["confirmed_later_peak_pct"] == 350
    assert report[0]["classification"] == "confirmed_missed_winner"
    assert report[0]["rule_that_blocked"] == "max_open"


def test_price_pct_5m_is_not_later_peak(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    row = {
        "address": "HOT",
        "discovered_via": "pumpfun",
        "price_pct_5m": 900,
        "reason": "late_funnel",
    }
    (metrics / "candidate_outcomes.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (metrics / "runtime_events.jsonl").write_text("", encoding="utf-8")
    report = build_missed_pumps(tmp_path)
    assert report[0]["classification"] == "hot_seen_not_bought"
    assert report[0]["confirmed_later_peak_pct"] is None
    assert report[0]["later_max_pnl_pct"] is None


def test_bought_token_not_reported_as_missed(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    row = {"address": "BOUGHT", "price_pct_5m": 500, "max_pnl_pct": 300}
    buy = {"event_type": "buy", "address": "BOUGHT"}
    (metrics / "candidate_outcomes.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (metrics / "runtime_events.jsonl").write_text(json.dumps(buy) + "\n", encoding="utf-8")
    assert build_missed_pumps(tmp_path) == []


def test_shadow_loser_is_confirmed_avoided_loser(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    row = {"address": "LOSER", "price_pct_5m": 10, "pnl_pct": -35, "reason": "risk_guard_high", "sample_type": "shadow_close"}
    (metrics / "candidate_outcomes.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (metrics / "runtime_events.jsonl").write_text("", encoding="utf-8")
    report = build_missed_pumps(tmp_path)
    assert report[0]["classification"] == "confirmed_avoided_loser"
