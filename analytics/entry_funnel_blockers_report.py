from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Iterable

from analytics.report_utils import (
    address_of,
    boolish,
    bought_addresses,
    fnum,
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
SAMPLES_JSON = "entry_funnel_blocker_samples.json"
BLOCKERS = (
    "rank_below_min",
    "soft_score",
    "vol_low",
    "mcap_low",
    "toxic_initial_sell_pressure",
    "untagged_buy_blocked",
    "momentum_ignition_needs_confirmation",
    "rebound_no_confirmation",
    "no_route",
    "proxy_liquidity",
    "trend_missing_without_second_tick",
)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _reason(row: dict[str, Any]) -> str:
    return _norm(row.get("reason") or row.get("reject_reason") or row.get("blocked_reason"))


def _event(row: dict[str, Any]) -> str:
    return _norm(row.get("event_type") or row.get("event") or row.get("source"))


def _decision_action(row: dict[str, Any]) -> str:
    return _norm(row.get("decision_action") or row.get("action") or row.get("decision"))


def _pnl(row: dict[str, Any]) -> float:
    return fnum(row.get("shadow_pnl_pct") or row.get("target_total_pnl_pct") or row.get("realized_pnl_pct") or row.get("pnl_pct"), 0.0)


def _peak(row: dict[str, Any]) -> float:
    return max(fnum(row.get("observed_peak_after_seen") or row.get("highest_pnl_pct") or row.get("peak_pnl_pct"), 0.0), _pnl(row))


def _matches_blocker(row: dict[str, Any], blocker: str) -> bool:
    reason = _reason(row)
    if blocker == "no_route":
        return "no_route" in reason or "route_required" in reason
    if blocker == "proxy_liquidity":
        return "proxy_liquidity" in reason or "liq_proxy" in reason
    if blocker == "trend_missing_without_second_tick":
        return "trend_missing_without_second_tick" in reason
    if blocker == "rebound_no_confirmation":
        return "rebound_no_confirmation" in reason or "shadow_rebound_watch" in reason
    return blocker in reason


def _recommended_action(blocker: str, rows: list[dict[str, Any]]) -> str:
    max_peak = max((_peak(row) for row in rows), default=0.0)
    avg_pnl = sum(_pnl(row) for row in rows) / len(rows) if rows else 0.0
    if blocker in {"toxic_initial_sell_pressure", "untagged_buy_blocked"}:
        return "keep_block"
    if blocker in {"proxy_liquidity", "trend_missing_without_second_tick"} and max_peak >= 500.0:
        return "micro_probe"
    if blocker in {"momentum_ignition_needs_confirmation", "rank_below_min"} and avg_pnl > 0.0:
        return "allow_small_paper"
    if max_peak >= 100.0:
        return "relax_to_shadow"
    return "needs_more_data"


def _blocker_detail(blocker: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    matched = [row for row in rows if _matches_blocker(row, blocker)]
    pnls = [_pnl(row) for row in matched]
    max_peak = max((_peak(row) for row in matched), default=0.0)
    return {
        "count": len({address_of(row) for row in matched if address_of(row)}) or len(matched),
        "sample_tokens": [
            {
                "address": address_of(row),
                "reason": _reason(row),
                "entry_lane": row.get("entry_lane"),
                "pnl_pct": _pnl(row),
                "peak_pct": _peak(row),
            }
            for row in matched[:10]
        ],
        "avg_shadow_pnl": round(sum(pnls) / len(pnls), 3) if pnls else 0.0,
        "max_shadow_peak": round(max_peak, 3),
        "would_pass_if_relaxed": len(matched),
        "recommended_action": _recommended_action(blocker, matched),
    }


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
    report["blocker_details"] = {blocker: _blocker_detail(blocker, all_rows) for blocker in BLOCKERS}
    return report


def write_entry_funnel_blockers_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_entry_funnel_blockers_report(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    write_json(metrics_dir(root) / SAMPLES_JSON, report.get("blocker_details", {}))
    return report


__all__ = [
    "REPORT_JSON",
    "SAMPLES_JSON",
    "build_entry_funnel_blockers_report",
    "write_entry_funnel_blockers_report",
]
