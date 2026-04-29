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
    }
    (metrics / "candidate_outcomes.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (metrics / "runtime_events.jsonl").write_text("", encoding="utf-8")
    report = build_missed_pumps(tmp_path)
    assert report[0]["later_max_pnl_pct"] == 350
    assert report[0]["rule_that_blocked"] == "max_open"
