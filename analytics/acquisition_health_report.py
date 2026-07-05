from __future__ import annotations

import collections
import datetime as dt
import json
from pathlib import Path
from typing import Any

from analytics.current_run import current_run_identity, filter_current_run_rows, parse_time, row_time
from analytics.pump_entry_lane_selector import select_pump_entry_lane
from analytics.report_utils import (
    address_of,
    boolish,
    fnum,
    load_candidate_outcomes,
    load_deduped_positions,
    load_runtime_events,
    metrics_dir,
    write_json,
)
from config.config import PROJECT_ROOT
from ml.lane_taxonomy import (
    LANE_MOONSHOT_MICRO_LOTTERY,
    LANE_PAPER_BOOTSTRAP_MICRO,
    LANE_PAPER_EXPLORATION_MICRO,
    LANE_RESEARCH_RANK_CANARY,
    LANE_SHADOW_FOLLOWUP_MICRO,
    LANE_SNIPER_RESEARCH_MICRO_FALLBACK,
    normalize_entry_lane,
)
from research_loop.api_budget import build_api_budget_report


REPORT_JSON = "acquisition_health_report.json"
RECOMMENDED_ACTIONS = {
    "keep_current",
    "open_rank_micro",
    "open_shadow_followup",
    "open_moonshot_micro",
    "open_paper_bootstrap",
    "reduce_api_pressure",
    "disable_losing_lane",
    "needs_more_data",
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


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
        _first(row, "reason", "green_sniper_reason", "entry_reason", "reject_reason", "blocked_reason", "exit_reason")
        or ""
    ).strip().lower()


def _lane(row: dict[str, Any]) -> str:
    return normalize_entry_lane(_first(row, "entry_lane", "lane", "profit_lane_tier", "size_bucket"))


def _bought(row: dict[str, Any]) -> bool:
    return _event(row) in {"buy", "bought", "paper_buy", "buy_ok"} or boolish(_first(row, "bought", "paper_buy"), False)


def _shadow(row: dict[str, Any]) -> bool:
    text = " ".join(
        str(_first(row, key) or "")
        for key in ("sample_type", "reason", "green_sniper_reason", "entry_reason", "action", "shadow_kind")
    ).lower()
    return "shadow" in text


def _requeue(row: dict[str, Any]) -> bool:
    text = f"{_event(row)} {_reason(row)}".lower()
    return "requeue" in text or "requeued" in text


def _hours(identity: dict[str, Any], rows: list[dict[str, Any]]) -> float:
    started = parse_time(identity.get("run_started_at") or identity.get("selected_at"))
    latest = max((row_time(row) for row in rows if row_time(row) is not None), default=None)
    if started is None or latest is None:
        return 0.0
    return max((latest - started).total_seconds() / 3600.0, 0.0)


def _counter_dict(counter: collections.Counter[str], limit: int = 20) -> dict[str, int]:
    return {key: int(value) for key, value in counter.most_common(limit) if key}


def _metric(payload: dict[str, Any], key: str, default: float = 0.0) -> float:
    return fnum(payload.get(key), default)


def _summary_int(payload: dict[str, Any], key: str) -> int | None:
    if key not in payload or payload.get(key) is None:
        return None
    try:
        return int(float(payload.get(key)))
    except Exception:
        return None


def _sum_metrics(payload: dict[str, Any], *keys: str) -> int:
    return int(sum(_metric(payload, key) for key in keys))


def _api_budget_status(api_budget: dict[str, Any]) -> dict[str, Any]:
    api_429_count = (
        _metric(api_budget, "gecko_429_count")
        + _metric(api_budget, "birdeye_429_count")
        + _metric(api_budget, "jupiter_rate_limit_count")
    )
    provider_degraded = _metric(api_budget, "provider_degraded_minutes")
    rpc_errors = _metric(api_budget, "rpc_errors")
    status = "warn" if api_429_count > 0 or provider_degraded > 0 else "ok"
    return {
        "status": status,
        "api_429_count": int(api_429_count),
        "provider_degraded_minutes": int(provider_degraded),
        "rpc_errors": int(rpc_errors),
        "cooldown_count": int(_metric(api_budget, "cooldown_count")),
        "sources": api_budget.get("sources") if isinstance(api_budget.get("sources"), dict) else {},
    }


def _selector_counts(rows: list[dict[str, Any]]) -> tuple[int, dict[str, int], dict[str, int]]:
    selected: collections.Counter[str] = collections.Counter()
    shadowed: collections.Counter[str] = collections.Counter()
    for row in rows:
        try:
            decision = select_pump_entry_lane(row)
        except Exception:
            continue
        if decision.allowed:
            selected[decision.selected_lane] += 1
        else:
            shadowed[decision.reason] += 1
    return sum(selected.values()), _counter_dict(selected), _counter_dict(shadowed)


def _position_opened(row: dict[str, Any]) -> bool:
    return _bought(row) or _first(row, "opened_at", "entry_price_usd", "buy_price_usd") is not None


def _recommended_action(
    *,
    api_budget: dict[str, Any],
    buys: int,
    actual_paper_buy_attempts: int,
    strategy_decisions: int,
    raw_discovered: int,
    shadow_followup_triggers: int,
    shadow_followup_buys: int,
    rank_canary_allowed: int,
    rank_canary_buys: int,
    moonshot_candidates: int,
    autotune: dict[str, Any],
) -> tuple[str, str]:
    if api_budget.get("status") == "warn":
        return "reduce_api_pressure", "api_budget_warn"
    autotune_actions = [str((action or {}).get("action") or "") for action in autotune.get("actions") or [] if isinstance(action, dict)]
    if any(action.startswith("disable") or action.endswith("shadow_only") for action in autotune_actions):
        return "disable_losing_lane", "autotune_detected_losing_lane"
    if raw_discovered < 5 and strategy_decisions < 5:
        return "needs_more_data", "insufficient_current_run_sample"
    if buys == 0 and shadow_followup_triggers > shadow_followup_buys:
        return "open_shadow_followup", "shadow_followup_triggers_not_bought"
    if buys == 0 and rank_canary_allowed > rank_canary_buys:
        return "open_rank_micro", "rank_canary_allowed_not_bought"
    if buys == 0 and moonshot_candidates > 0:
        return "open_moonshot_micro", "moonshot_candidates_not_bought"
    if buys == 0 and actual_paper_buy_attempts <= 0:
        return "open_paper_bootstrap", "no_actual_paper_buy_attempts"
    if buys == 0:
        return "needs_more_data", "no_buys_no_clear_micro_trigger"
    return "keep_current", "acquisition_flow_has_buys"


def build_acquisition_health_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    base = metrics_dir(root)
    runtime_rows_raw = load_runtime_events(root)
    identity = current_run_identity(root, runtime_rows_raw)
    runtime_rows = filter_current_run_rows(runtime_rows_raw, identity)
    outcome_rows = filter_current_run_rows(load_candidate_outcomes(root), identity)
    position_rows = filter_current_run_rows(load_deduped_positions(root), identity)
    all_rows = runtime_rows + outcome_rows + position_rows

    current_summary = _read_json(base / "current_run_summary.json")
    selector_report = _read_json(base / "pump_entry_lane_selector_report.json")
    shadow_followup_report = _read_json(base / "shadow_followup_micro_report.json")
    paper_exploration_report = _read_json(base / "paper_exploration_quota_report.json")
    rank_report = _read_json(base / "research_rank_current_run_report.json")
    moonshot_report = _read_json(base / "moonshot_micro_lottery_report.json")
    autotune = _read_json(base / "current_run_autotune_state.json")

    raw_discovered = int(
        _summary_int(current_summary, "raw_discovered")
        if _summary_int(current_summary, "raw_discovered") is not None
        else len({address_of(row) for row in all_rows if address_of(row)})
    )
    strategy_decisions = int(
        _summary_int(current_summary, "strategy_decisions")
        if _summary_int(current_summary, "strategy_decisions") is not None
        else sum(1 for row in runtime_rows if _event(row) in {"strategy_decision", "candidate_decision"})
    )
    runtime_actual_buys = sum(1 for row in runtime_rows if _event(row) == "actual_paper_buy")
    runtime_buy_events = sum(1 for row in runtime_rows if _bought(row))
    position_buys = sum(1 for row in position_rows if _position_opened(row))
    summary_buys = _summary_int(current_summary, "actual_paper_buys")
    if summary_buys is None:
        summary_buys = _summary_int(current_summary, "buys")
    buys = int(summary_buys if summary_buys is not None else max(runtime_actual_buys, runtime_buy_events, position_buys))
    hours = _hours(identity, all_rows)
    buys_per_hour = round(buys / hours, 6) if hours > 0 else 0.0
    shadows = int(current_summary.get("shadows") or sum(1 for row in all_rows if _shadow(row)))
    requeues = sum(1 for row in all_rows if _requeue(row))
    actual_paper_buy_attempts = int(
        _summary_int(current_summary, "actual_paper_buy_attempts")
        if _summary_int(current_summary, "actual_paper_buy_attempts") is not None
        else sum(1 for row in runtime_rows if _event(row) == "actual_paper_buy_attempt")
    )
    actual_paper_buys = int(
        _summary_int(current_summary, "actual_paper_buys")
        if _summary_int(current_summary, "actual_paper_buys") is not None
        else runtime_actual_buys
    )
    blocked_before_buy = int(
        _summary_int(current_summary, "blocked_before_buy")
        if _summary_int(current_summary, "blocked_before_buy") is not None
        else sum(1 for row in runtime_rows if _event(row) == "blocked_before_buy")
    )
    shadow_only = int(
        _summary_int(current_summary, "shadow_only")
        if _summary_int(current_summary, "shadow_only") is not None
        else sum(1 for row in all_rows if _shadow(row) and _event(row) != "actual_paper_buy")
    )

    selector_allowed, selected_by_lane, selector_shadow = _selector_counts(runtime_rows + outcome_rows)
    selected_from_report = selector_report.get("selected_by_lane") if isinstance(selector_report.get("selected_by_lane"), dict) else {}
    shadow_from_report = selector_report.get("shadow_by_reason") if isinstance(selector_report.get("shadow_by_reason"), dict) else {}
    if selected_from_report:
        selected_by_lane = {str(key): int(value) for key, value in selected_from_report.items()}
        selector_allowed = sum(selected_by_lane.values())
    if shadow_from_report:
        selector_shadow = {str(key): int(value) for key, value in shadow_from_report.items()}

    blocker_counter = collections.Counter(_reason(row) for row in all_rows if _reason(row))
    blocker_counter.update({str(key): int(value) for key, value in selector_shadow.items()})
    top_blockers = _counter_dict(blocker_counter, 20)

    shadow_followup_triggers = int(_metric(shadow_followup_report, "micro_triggers"))
    paper_exploration_eligible = int(_metric(paper_exploration_report, "eligible_shadows"))
    paper_exploration_buys = int(_metric(paper_exploration_report, "quota_buys"))
    rank_canary_allowed = _sum_metrics(rank_report, "normal_micro_seen", "priority_seen")
    if rank_canary_allowed <= 0:
        rank_canary_allowed = sum(1 for row in all_rows if _lane(row) == LANE_RESEARCH_RANK_CANARY)
    rank_canary_buys = _sum_metrics(rank_report, "normal_micro_bought", "priority_bought")
    if rank_canary_buys <= 0:
        rank_canary_buys = sum(1 for row in all_rows if _lane(row) == LANE_RESEARCH_RANK_CANARY and _bought(row))
    moonshot_candidates = int(_metric(moonshot_report, "candidates_seen"))
    buy_rows = position_rows if position_rows else runtime_rows
    shadow_followup_buys = sum(1 for row in buy_rows if _lane(row) == LANE_SHADOW_FOLLOWUP_MICRO and _position_opened(row))
    moonshot_buys = sum(1 for row in buy_rows if _lane(row) == LANE_MOONSHOT_MICRO_LOTTERY and _position_opened(row))
    sniper_fallback_buys = sum(1 for row in buy_rows if _lane(row) == LANE_SNIPER_RESEARCH_MICRO_FALLBACK and _position_opened(row))
    paper_exploration_lane_buys = sum(1 for row in buy_rows if _lane(row) == LANE_PAPER_EXPLORATION_MICRO and _position_opened(row))
    paper_bootstrap_buys = sum(1 for row in buy_rows if _lane(row) == LANE_PAPER_BOOTSTRAP_MICRO and _position_opened(row))
    micro_triggers = (
        shadow_followup_triggers
        + paper_exploration_eligible
        + rank_canary_allowed
        + int(moonshot_report.get("confirmed_moonshot_buy") or 0)
    )

    api_budget = _api_budget_status(build_api_budget_report(root, write=False))
    recommended_action, recommended_reason = _recommended_action(
        api_budget=api_budget,
        buys=buys,
        actual_paper_buy_attempts=actual_paper_buy_attempts,
        strategy_decisions=strategy_decisions,
        raw_discovered=raw_discovered,
        shadow_followup_triggers=shadow_followup_triggers,
        shadow_followup_buys=shadow_followup_buys,
        rank_canary_allowed=rank_canary_allowed,
        rank_canary_buys=rank_canary_buys,
        moonshot_candidates=moonshot_candidates,
        autotune=autotune,
    )

    return {
        "generated_at_utc": _utc_now(),
        "current_run_id": str(identity.get("run_id") or "legacy"),
        "current_run": identity,
        "raw_discovered": raw_discovered,
        "strategy_decisions": strategy_decisions,
        "buys": buys,
        "actual_paper_buy_attempts": actual_paper_buy_attempts,
        "actual_paper_buys": actual_paper_buys,
        "shadow_only": shadow_only,
        "blocked_before_buy": blocked_before_buy,
        "buys_per_hour": round(float(buys_per_hour), 6),
        "shadows": shadows,
        "requeues": int(requeues),
        "top_blockers": top_blockers,
        "allowed_by_selector": int(selector_allowed),
        "selected_by_lane": selected_by_lane,
        "micro_triggers": int(micro_triggers),
        "paper_exploration_eligible": paper_exploration_eligible,
        "paper_exploration_buys": max(paper_exploration_buys, paper_exploration_lane_buys),
        "paper_bootstrap_buys": paper_bootstrap_buys,
        "rank_canary_allowed": rank_canary_allowed,
        "rank_canary_buys": rank_canary_buys,
        "moonshot_candidates": moonshot_candidates,
        "moonshot_buys": moonshot_buys,
        "shadow_followup_triggers": shadow_followup_triggers,
        "shadow_followup_buys": shadow_followup_buys,
        "sniper_research_micro_fallback_buys": sniper_fallback_buys,
        "api_budget_status": api_budget,
        "recommended_action": recommended_action,
        "recommended_reason": recommended_reason,
        "recommendation_values": sorted(RECOMMENDED_ACTIONS),
        "autotune_actions": autotune.get("actions") if isinstance(autotune.get("actions"), list) else [],
        "source_rows": {
            "runtime_events": len(runtime_rows),
            "candidate_outcomes": len(outcome_rows),
            "positions": len(position_rows),
        },
    }


def write_acquisition_health_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_acquisition_health_report(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    return report


__all__ = [
    "REPORT_JSON",
    "RECOMMENDED_ACTIONS",
    "build_acquisition_health_report",
    "write_acquisition_health_report",
]
