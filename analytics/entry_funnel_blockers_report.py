from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Iterable

from analytics.report_utils import (
    address_of,
    boolish,
    bought_addresses,
    load_candidate_outcomes,
    load_paper_positions,
    load_runtime_events,
    load_sqlite_positions,
    metrics_dir,
    read_jsonl,
    write_json,
)
from config.config import PROJECT_ROOT


REPORT_JSON = "entry_funnel_blockers_report.json"


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _reason(row: dict[str, Any]) -> str:
    return _norm(row.get("reason") or row.get("reject_reason") or row.get("blocked_reason"))


def _event(row: dict[str, Any]) -> str:
    return _norm(row.get("event_type") or row.get("event") or row.get("source"))


def _decision_action(row: dict[str, Any]) -> str:
    return _norm(row.get("decision_action") or row.get("action") or row.get("decision"))


def _unique_matching(rows: Iterable[dict[str, Any]], predicate: Any) -> set[str]:
    out: set[str] = set()
    for row in rows:
        try:
            if not predicate(row):
                continue
        except Exception:
            continue
        addr = address_of(row)
        if addr:
            out.add(addr)
    return out


def build_entry_funnel_blockers_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    metrics = metrics_dir(root)
    runtime_rows = load_runtime_events(root)
    ledger_rows = read_jsonl(metrics / "decision_ledger.jsonl")
    outcome_rows = load_candidate_outcomes(root)
    position_rows = load_paper_positions(root) + load_sqlite_positions(root)
    all_rows = runtime_rows + ledger_rows + outcome_rows + position_rows

    raw_seen = {address_of(row) for row in all_rows if address_of(row)}
    strategy_decisions = [
        row for row in runtime_rows if _event(row) == "strategy_decision"
    ]
    bought = bought_addresses(root)

    def reason_contains(text: str) -> Any:
        return lambda row: text in _reason(row)

    report = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "raw_seen": len(raw_seen),
        "strategy_decisions": len(strategy_decisions),
        "bought": len(bought),
        "blocked_by_rank_below_min": len(
            _unique_matching(
                runtime_rows + ledger_rows,
                lambda row: _reason(row) == "rank_below_min"
                or (_event(row) == "research_rank_canary_eval" and _reason(row) == "rank_below_min"),
            )
        ),
        "blocked_by_soft_score": len(_unique_matching(ledger_rows, reason_contains("soft_score"))),
        "blocked_by_vol_low": len(_unique_matching(ledger_rows, reason_contains("vol_low"))),
        "blocked_by_mcap_low": len(_unique_matching(ledger_rows, reason_contains("mcap_low"))),
        "blocked_by_untagged": len(_unique_matching(ledger_rows, reason_contains("untagged_buy_blocked"))),
        "blocked_by_toxic_initial_sell_pressure": len(
            _unique_matching(ledger_rows + runtime_rows, reason_contains("toxic_initial_sell_pressure"))
        ),
        "blocked_by_momentum_ignition_toxic_filter": len(
            _unique_matching(ledger_rows + runtime_rows + outcome_rows, reason_contains("momentum_ignition_toxic_filter"))
        ),
        "rank_canary_allowed": len(
            _unique_matching(
                runtime_rows + ledger_rows + position_rows,
                lambda row: (_event(row) == "research_rank_canary_eval" and boolish(row.get("allowed"), False))
                or _norm(row.get("entry_lane")) == "pump_early_research_rank_canary"
                or _norm(row.get("gate_profile")) == "research_rank_canary",
            )
        ),
        "momentum_ignition_allowed": len(
            _unique_matching(
                ledger_rows + outcome_rows + position_rows,
                lambda row: _norm(row.get("entry_subprofile") or row.get("sniper_research_subprofile"))
                == "sniper_research_momentum_ignition",
            )
        ),
        "rebound_confirmed": len(
            _unique_matching(
                ledger_rows + outcome_rows + position_rows,
                lambda row: boolish(row.get("pumpswap_rebound_confirmation"), False)
                or _norm(row.get("entry_lane")) == "pump_early_pumpswap_rebound_prime"
                or (
                    _norm(row.get("gate_profile")) == "pumpswap_rebound_prime"
                    and _decision_action(row) in {"bought", "buy", "live"}
                ),
            )
        ),
        "birth_micro_allowed": len(
            _unique_matching(
                ledger_rows + outcome_rows + position_rows,
                lambda row: _norm(row.get("entry_lane")) == "pump_early_birth_probe_micro_canary"
                and _decision_action(row) in {"bought", "buy", "live", ""},
            )
        ),
        "source_rows": {
            "runtime_events": len(runtime_rows),
            "decision_ledger": len(ledger_rows),
            "candidate_outcomes": len(outcome_rows),
            "positions": len(position_rows),
        },
    }
    return report


def write_entry_funnel_blockers_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_entry_funnel_blockers_report(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    return report


__all__ = [
    "REPORT_JSON",
    "build_entry_funnel_blockers_report",
    "write_entry_funnel_blockers_report",
]
