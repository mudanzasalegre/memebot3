from __future__ import annotations

import collections
import datetime as dt
import statistics
from pathlib import Path
from typing import Any

from analytics.current_run import current_run_identity, filter_current_run_rows, parse_time, row_time
from analytics.lane_policy_categories import classify_policy_category
from analytics.report_utils import (
    address_of,
    boolish,
    fnum,
    load_candidate_outcomes,
    load_paper_positions,
    load_runtime_events,
    load_sqlite_positions,
    metrics_dir,
    write_json,
)
from config.config import PROJECT_ROOT
from ml.lane_taxonomy import normalize_entry_lane


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _event(row: dict[str, Any]) -> str:
    return str(_first(row, "event_type", "event", "action", "decision_action") or "").strip().lower()


def _reason(row: dict[str, Any]) -> str:
    return str(
        _first(row, "reason", "green_sniper_reason", "reject_reason", "blocked_reason", "exit_reason")
        or ""
    ).strip()


def _lane(row: dict[str, Any]) -> str:
    return normalize_entry_lane(_first(row, "entry_lane", "lane", "profit_lane_tier", "size_bucket"))


def _pnl(row: dict[str, Any]) -> float:
    return fnum(_first(row, "total_pnl_pct", "realized_pnl_pct", "pnl_pct", "target_total_pnl_pct"), 0.0)


def _pnl_usd(row: dict[str, Any]) -> float:
    return fnum(_first(row, "total_pnl_usd", "realized_pnl_usd", "pnl_usd"), 0.0)


def _peak(row: dict[str, Any]) -> float:
    return max(
        fnum(_first(row, "highest_pnl_pct", "max_pnl_pct_seen", "peak_pnl_pct", "observed_peak_after_seen"), 0.0),
        _pnl(row),
    )


def _closed(row: dict[str, Any]) -> bool:
    if boolish(row.get("closed"), False):
        return True
    if _first(row, "closed_at", "exit_reason", "total_pnl_pct", "realized_pnl_pct") is not None:
        return True
    return _event(row) in {"trade_close", "close", "closed"}


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_pnl(row) for row in rows if _first(row, "total_pnl_pct", "realized_pnl_pct", "pnl_pct", "target_total_pnl_pct") is not None]
    return {
        "rows": len(rows),
        "pnl_rows": len(pnls),
        "win_rate_pct": round(100.0 * sum(1 for value in pnls if value > 0.0) / len(pnls), 3) if pnls else 0.0,
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3) if pnls else 0.0,
        "median_pnl_pct": round(statistics.median(pnls), 3) if pnls else 0.0,
        "total_pnl_pct_points": round(sum(pnls), 3) if pnls else 0.0,
        "severe_losses": sum(1 for value in pnls if value <= -25.0),
        "peak_100": sum(1 for row in rows if _peak(row) >= 100.0),
        "peak_500": sum(1 for row in rows if _peak(row) >= 500.0),
        "peak_1000": sum(1 for row in rows if _peak(row) >= 1000.0),
    }


def _current_rows(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    runtime_rows = load_runtime_events(root)
    identity = current_run_identity(root, runtime_rows)
    outcomes = load_candidate_outcomes(root)
    positions = load_paper_positions(root) + load_sqlite_positions(root)
    return (
        identity,
        filter_current_run_rows(runtime_rows, identity),
        filter_current_run_rows(outcomes, identity),
        filter_current_run_rows(positions, identity),
    )


def build_current_run_trade_diagnostics(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    identity, runtime_rows, outcome_rows, position_rows = _current_rows(root)
    shadow_rows = [
        row
        for row in outcome_rows + runtime_rows
        if "shadow" in " ".join(str(_first(row, key) or "") for key in ("sample_type", "reason", "action", "shadow_kind")).lower()
    ]
    candidate_decisions = [
        row
        for row in runtime_rows + outcome_rows
        if _event(row) in {"candidate_decision", "strategy_decision", "research_rank_canary_eval"}
        or str(_first(row, "stage", "sample_type") or "").strip().lower() in {"candidate_decision", "candidate_outcome"}
    ]
    groups: dict[str, dict[str, Any]] = {}
    for name, rows in (
        ("real_paper_positions", position_rows),
        ("shadow_outcomes", shadow_rows),
        ("candidate_decisions", candidate_decisions),
    ):
        groups[name] = _summary(rows)
    return {
        "generated_at_utc": _now(),
        "current_run": identity,
        "real_paper_positions": groups["real_paper_positions"],
        "shadow_outcomes": groups["shadow_outcomes"],
        "candidate_decisions": {
            **groups["candidate_decisions"],
            "actions": dict(collections.Counter(_event(row) or "unknown" for row in candidate_decisions).most_common(20)),
            "reasons": dict(collections.Counter(_reason(row) or "unknown" for row in candidate_decisions).most_common(20)),
        },
        "by_lane": _lane_summary(runtime_rows, outcome_rows, position_rows),
        "samples": {
            "positions": [_sample(row) for row in position_rows[:25]],
            "shadows": [_sample(row) for row in shadow_rows[:25]],
            "candidate_decisions": [_sample(row) for row in candidate_decisions[:25]],
        },
    }


def _sample(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "address": address_of(row),
        "ts_utc": _first(row, "ts_utc", "timestamp", "opened_at", "closed_at", "created_at"),
        "event_type": _event(row),
        "lane": _lane(row),
        "reason": _reason(row),
        "pnl_pct": _pnl(row),
        "peak_pct": _peak(row),
    }


def _lane_summary(
    runtime_rows: list[dict[str, Any]],
    outcome_rows: list[dict[str, Any]],
    position_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in runtime_rows + outcome_rows + position_rows:
        grouped[_lane(row)].append(row)
    out: dict[str, Any] = {}
    for lane, rows in sorted(grouped.items()):
        events = [_event(row) for row in rows]
        out[lane] = {
            **_summary(rows),
            "buys": sum(1 for event in events if event in {"buy", "bought", "paper_buy", "buy_ok"}),
            "shadows": sum(1 for row in rows if "shadow" in " ".join(str(_first(row, key) or "") for key in ("sample_type", "reason", "action", "shadow_kind")).lower()),
            "policy_category": classify_policy_category(rows[-1]) if rows else "unknown",
        }
    return out


def build_current_run_funnel(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    identity, runtime_rows, outcome_rows, position_rows = _current_rows(root)
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in runtime_rows + outcome_rows + position_rows:
        addr = address_of(row)
        if addr:
            grouped[addr].append(row)
    final_states: dict[str, int] = {}
    blockers: dict[str, int] = {}
    rows_out: list[dict[str, Any]] = []
    for addr, rows in grouped.items():
        rows.sort(key=lambda row: str(_first(row, "ts_utc", "timestamp", "opened_at", "closed_at", "created_at") or ""))
        bought = any(_event(row) in {"buy", "bought", "paper_buy", "buy_ok"} for row in rows) or any(row in position_rows for row in rows)
        shadow = any("shadow" in " ".join(str(_first(row, key) or "") for key in ("sample_type", "reason", "action", "shadow_kind")).lower() for row in rows)
        state = "bought" if bought else "shadow" if shadow else "rejected"
        reason = next((_reason(row) for row in reversed(rows) if _reason(row)), state)
        final_states[state] = final_states.get(state, 0) + 1
        blockers[reason] = blockers.get(reason, 0) + 1
        rows_out.append(
            {
                "address": addr,
                "final_state": state,
                "final_blocking_reason": reason,
                "events": len(rows),
                "confirmed_later_peak_pct": max((_peak(row) for row in rows), default=0.0),
            }
        )
    return {
        "generated_at_utc": _now(),
        "current_run": identity,
        "rows": sorted(rows_out, key=lambda row: row["address"]),
        "summary": {
            "candidates": len(rows_out),
            "final_states": dict(sorted(final_states.items())),
            "top_blockers": dict(collections.Counter(blockers).most_common(20)),
        },
    }


def build_current_run_missed_pumps(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    identity, runtime_rows, outcome_rows, position_rows = _current_rows(root)
    bought = {address_of(row) for row in runtime_rows + position_rows if address_of(row) and (_event(row) in {"buy", "bought", "paper_buy", "buy_ok"} or row in position_rows)}
    missed = []
    for row in outcome_rows:
        addr = address_of(row)
        if not addr or addr in bought:
            continue
        peak = _peak(row)
        if peak >= 100.0 or fnum(_first(row, "price_pct_5m", "buy_price_pct_5m"), 0.0) >= 100.0:
            missed.append(
                {
                    "address": addr,
                    "symbol": row.get("symbol"),
                    "lane": _lane(row),
                    "reason": _reason(row),
                    "peak_pct": peak,
                    "pnl_pct": _pnl(row),
                    "price_pct_5m_at_seen": _first(row, "price_pct_5m", "buy_price_pct_5m"),
                    "classification": "confirmed_missed_winner" if peak >= 100.0 else "hot_seen_not_bought",
                }
            )
    missed.sort(key=lambda row: (-fnum(row.get("peak_pct"), 0.0), str(row.get("address") or "")))
    return {
        "generated_at_utc": _now(),
        "current_run": identity,
        "rows": missed,
        "summary": {
            "missed": len(missed),
            "peak_100": sum(1 for row in missed if fnum(row.get("peak_pct"), 0.0) >= 100.0),
            "peak_500": sum(1 for row in missed if fnum(row.get("peak_pct"), 0.0) >= 500.0),
            "peak_1000": sum(1 for row in missed if fnum(row.get("peak_pct"), 0.0) >= 1000.0),
        },
    }


def build_current_run_lane_summary(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    identity, runtime_rows, outcome_rows, position_rows = _current_rows(root)
    return {
        "generated_at_utc": _now(),
        "current_run": identity,
        "lanes": _lane_summary(runtime_rows, outcome_rows, position_rows),
    }


def _hours(identity: dict[str, Any], rows: list[dict[str, Any]]) -> float:
    started = parse_time(identity.get("run_started_at") or identity.get("selected_at"))
    latest = max((row_time(row) for row in rows if row_time(row) is not None), default=None)
    if started is None or latest is None:
        return 0.0
    return max((latest - started).total_seconds() / 3600.0, 1.0 / 60.0)


def build_bot_profitability_health(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    identity, runtime_rows, outcome_rows, position_rows = _current_rows(root)
    closed = [row for row in position_rows if _closed(row)]
    pnls = [_pnl(row) for row in closed]
    total_usd = sum(_pnl_usd(row) for row in closed)
    hours = _hours(identity, runtime_rows + outcome_rows + position_rows)
    buys = sum(1 for row in runtime_rows if _event(row) in {"buy", "bought", "paper_buy", "buy_ok"}) + len(position_rows)
    shadows = [
        row
        for row in runtime_rows + outcome_rows
        if "shadow" in " ".join(str(_first(row, key) or "") for key in ("sample_type", "reason", "action", "shadow_kind")).lower()
    ]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)
    recent_shadows = [row for row in shadows if (row_time(row) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) >= cutoff]
    best_shadow = max(recent_shadows, key=lambda row: _peak(row), default=None)
    blockers = collections.Counter(_reason(row) for row in runtime_rows + outcome_rows if _reason(row))
    primary_blocker = blockers.most_common(1)[0][0] if blockers else ""
    win_rate = 100.0 * sum(1 for value in pnls if value > 0.0) / len(pnls) if pnls else 0.0
    missed = [_peak(row) for row in outcome_rows if address_of(row)]
    action = "keep_paper_running"
    if closed and win_rate < 40.0:
        action = "reduce_size_and_follow_shadows"
    if not closed and len(shadows) > buys:
        action = "allow_idle_micro_exploration"
    if primary_blocker in {"untagged_buy_blocked", "pumpswap_strict_no_sublane"}:
        action = "inspect_entry_lane_selector"
    return {
        "generated_at_utc": _now(),
        "current_run": identity,
        "current_run_closed_trades": len(closed),
        "current_run_win_rate": round(win_rate, 3),
        "current_run_avg_pnl": round(sum(pnls) / len(pnls), 3) if pnls else 0.0,
        "current_run_median_pnl": round(statistics.median(pnls), 3) if pnls else 0.0,
        "current_run_total_usd": round(total_usd, 6),
        "buys_per_hour": round(buys / hours, 3) if hours else 0.0,
        "shadows_per_hour": round(len(shadows) / hours, 3) if hours else 0.0,
        "missed_peak_100_500_1000": {
            "peak_100": sum(1 for value in missed if value >= 100.0),
            "peak_500": sum(1 for value in missed if value >= 500.0),
            "peak_1000": sum(1 for value in missed if value >= 1000.0),
        },
        "best_shadow_candidate_last_3h": _sample(best_shadow) if best_shadow else None,
        "primary_blocker": primary_blocker,
        "recommended_next_action": action,
    }


def write_current_run_trade_diagnostics(root: Path | None = None) -> dict[str, Any]:
    report = build_current_run_trade_diagnostics(root)
    write_json(metrics_dir(root) / "current_run_trade_diagnostics.json", report)
    return report


def write_current_run_funnel(root: Path | None = None) -> dict[str, Any]:
    report = build_current_run_funnel(root)
    write_json(metrics_dir(root) / "current_run_funnel.json", report)
    return report


def write_current_run_missed_pumps(root: Path | None = None) -> dict[str, Any]:
    report = build_current_run_missed_pumps(root)
    write_json(metrics_dir(root) / "current_run_missed_pumps.json", report)
    return report


def write_current_run_lane_summary(root: Path | None = None) -> dict[str, Any]:
    report = build_current_run_lane_summary(root)
    write_json(metrics_dir(root) / "current_run_lane_summary.json", report)
    return report


def write_bot_profitability_health(root: Path | None = None) -> dict[str, Any]:
    report = build_bot_profitability_health(root)
    write_json(metrics_dir(root) / "bot_profitability_health.json", report)
    return report


__all__ = [
    "build_bot_profitability_health",
    "build_current_run_funnel",
    "build_current_run_lane_summary",
    "build_current_run_missed_pumps",
    "build_current_run_trade_diagnostics",
    "write_bot_profitability_health",
    "write_current_run_funnel",
    "write_current_run_lane_summary",
    "write_current_run_missed_pumps",
    "write_current_run_trade_diagnostics",
]
