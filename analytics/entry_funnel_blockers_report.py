from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
import re
from typing import Any, Iterable

from analytics.report_utils import (
    address_of,
    boolish,
    bought_addresses,
    load_candidate_outcomes,
    load_deduped_positions,
    load_runtime_events,
    metrics_dir,
    read_jsonl,
    write_json,
)
from config.config import PROJECT_ROOT


REPORT_JSON = "entry_funnel_blockers_report.json"
SAMPLES_JSON = "entry_funnel_blocker_samples.json"
BLOCKERS = (
    "missing_age",
    "snapshot_unavailable",
    "unconfirmed_entry_clock",
    "local_pumpfun_wait",
    "low_green_momentum",
    "low_txns_5m",
    "low_liquidity",
    "weak_buy_sell_ratio",
    "cluster_bad",
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
    return next((value for key in ("reason", "reject_reason", "blocked_reason")
                 if (value := _norm(row.get(key)))), "")


def _event(row: dict[str, Any]) -> str:
    return _norm(row.get("event_type") or row.get("event") or row.get("source"))


def _decision_action(row: dict[str, Any]) -> str:
    return _norm(row.get("decision_action") or row.get("action") or row.get("decision"))


def _shadow_outcome(row: dict[str, Any]) -> bool:
    return (_event(row) == "candidate_outcome"
            and "shadow" in _norm(row.get("shadow_kind")))


def _observed_number(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        # Explicit zero is an observation; invalid canonical data cannot be
        # repaired by a more optimistic alias or converted to a zero return.
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _pnl(row: dict[str, Any]) -> float | None:
    return (_observed_number(row, "shadow_pnl_pct", "pnl_pct")
            if _shadow_outcome(row) else None)


def _peak(row: dict[str, Any]) -> float | None:
    # An ending return is not an independently observed peak. Targets, position
    # PnL and untyped decision fields are not shadow-outcome observations.
    return (_observed_number(row, "observed_peak_after_seen", "highest_pnl_pct", "peak_pnl_pct")
            if _shadow_outcome(row) else None)


def _matches_blocker(row: dict[str, Any], blocker: str) -> bool:
    terms = set(re.split(r"[,;:|\s]+", _reason(row)))
    if blocker == "no_route":
        return bool(terms & {"no_route", "route_required"})
    if blocker == "proxy_liquidity":
        return bool(terms & {"proxy_liquidity", "liq_proxy"})
    if blocker == "trend_missing_without_second_tick":
        return "trend_missing_without_second_tick" in terms
    if blocker == "rebound_no_confirmation":
        return bool(terms & {"rebound_no_confirmation", "shadow_rebound_watch"})
    return blocker in terms


def _recommended_action(blocker: str, rows: list[dict[str, Any]]) -> str:
    if blocker in {"toxic_initial_sell_pressure", "untagged_buy_blocked"}:
        return "keep_block"
    # Removing one recorded blocker does not evaluate the remaining gates,
    # contemporaneous routing/depth, capital admission, exits or execution cost.
    return "needs_counterfactual_evaluation" if rows else "needs_more_data"


def _blocker_detail(blocker: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    matched = [row for row in rows if _matches_blocker(row, blocker)]
    pnls = [value for row in matched if (value := _pnl(row)) is not None]
    peaks = [value for row in matched if (value := _peak(row)) is not None]
    addresses = {address_of(row) for row in matched if address_of(row)}
    mean = sum(value / len(pnls) for value in pnls) if pnls else None
    mean = mean if mean is None or math.isfinite(mean) else None
    return {
        "count": len(addresses),
        "distinct_tokens": len(addresses),
        "matched_rows": len(matched),
        "unidentified_rows": sum(not address_of(row) for row in matched),
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
        "avg_shadow_pnl": round(mean, 3) if mean is not None else None,
        "max_shadow_peak": round(max(peaks), 3) if peaks else None,
        "observed_shadow_pnl_rows": len(pnls),
        "observed_shadow_peak_rows": len(peaks),
        "outcome_scope": "explicit_shadow_candidate_outcome_rows_not_net_financial_fills",
        "would_pass_if_relaxed": None,
        "eligibility_effect_established": False,
        "financial_evidence": False,
        "recommended_action_scope": "diagnostic_followup_only_not_policy_activation",
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
    position_rows = load_deduped_positions(root)
    all_rows = runtime_rows + ledger_rows + outcome_rows + position_rows

    raw_seen = {address_of(row) for row in all_rows if address_of(row)}
    strategy_decisions = [
        row for row in runtime_rows if _event(row) == "strategy_decision"
    ]
    blocked_rows = [row for row in runtime_rows if _event(row) == "blocked_before_buy"]
    recognized = sum(any(_matches_blocker(row, blocker) for blocker in BLOCKERS)
                     for row in blocked_rows)
    bought = bought_addresses(root)

    def reason_contains(text: str) -> Any:
        return lambda row: _matches_blocker(row, text)

    report = {
        "schema_version": 2,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "measurement_scope": "available_historical_records_not_a_current_run_or_offered_demand_census",
        "qualification": "Rows and distinct tokens have different grains; blocker groups overlap. Shadow observations do not prove executable counterfactual entries, checked net profits or strategy activation.",
        "raw_seen": len(raw_seen),
        "strategy_decisions": len(strategy_decisions),
        "blocking_observation_coverage": {
            "runtime_blocked_rows": len(blocked_rows),
            "runtime_blocked_distinct_tokens": len({address_of(row) for row in blocked_rows if address_of(row)}),
            "recognized_rows": recognized,
            "unrecognized_rows": len(blocked_rows) - recognized,
            "qualification": "Reason groups overlap; recognized does not mean causally explained or financially evaluated.",
        },
        "bought": len(bought),
        "blocked_by_rank_below_min": len(
            _unique_matching(
                runtime_rows + ledger_rows,
                lambda row: _matches_blocker(row, "rank_below_min"),
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
