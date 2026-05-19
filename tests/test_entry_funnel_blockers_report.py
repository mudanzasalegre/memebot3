from __future__ import annotations

import json
from pathlib import Path

from analytics.entry_funnel_blockers_report import write_entry_funnel_blockers_report


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_entry_funnel_blockers_report_counts_core_blockers(tmp_path: Path) -> None:
    metrics = tmp_path / "data" / "metrics"
    _write_jsonl(
        metrics / "runtime_events.jsonl",
        [
            {"event_type": "strategy_decision", "address": "A"},
            {"event_type": "research_rank_canary_eval", "address": "B", "allowed": False, "reason": "rank_below_min"},
            {"event_type": "research_rank_canary_eval", "address": "C", "allowed": True, "reason": "research_rank_canary"},
        ],
    )
    _write_jsonl(
        metrics / "decision_ledger.jsonl",
        [
            {"address": "D", "reason": "soft_score", "action": "rejected"},
            {"address": "E", "reason": "vol_low", "action": "wait"},
            {"address": "F", "reason": "mcap_low", "action": "wait"},
            {"address": "G", "reason": "untagged_buy_blocked", "action": "rejected"},
            {"address": "H", "reason": "toxic_initial_sell_pressure", "action": "shadow"},
            {"address": "I", "reason": "momentum_ignition_toxic_filter:momentum:cluster_bad", "action": "shadow"},
            {
                "address": "J",
                "entry_subprofile": "sniper_research_momentum_ignition",
                "action": "bought",
            },
            {
                "address": "K",
                "pumpswap_rebound_confirmation": 1,
                "action": "bought",
            },
        ],
    )
    portfolio = {"positions": [{"address": "L", "entry_lane": "pump_early_birth_probe_micro_canary"}]}
    (tmp_path / "data" / "paper_portfolio.json").write_text(json.dumps(portfolio), encoding="utf-8")

    report = write_entry_funnel_blockers_report(tmp_path)

    assert report["raw_seen"] == 12
    assert report["strategy_decisions"] == 1
    assert report["bought"] == 1
    assert report["blocked_by_rank_below_min"] == 1
    assert report["blocked_by_soft_score"] == 1
    assert report["blocked_by_vol_low"] == 1
    assert report["blocked_by_mcap_low"] == 1
    assert report["blocked_by_untagged"] == 1
    assert report["blocked_by_toxic_initial_sell_pressure"] == 1
    assert report["blocked_by_momentum_ignition_toxic_filter"] == 1
    assert report["rank_canary_allowed"] == 1
    assert report["momentum_ignition_allowed"] == 1
    assert report["rebound_confirmed"] == 1
    assert report["birth_micro_allowed"] == 1
    assert (metrics / "entry_funnel_blockers_report.json").exists()
