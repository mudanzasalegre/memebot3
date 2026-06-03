from __future__ import annotations

import collections
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analytics.lane_policy_categories import POLICY_SHADOW_FOLLOWUP_MICRO
from analytics.lane_sizing import LANE_SHADOW_FOLLOWUP_MICRO
from analytics.report_utils import (
    address_of,
    boolish,
    fnum,
    load_candidate_outcomes,
    load_runtime_events,
    metrics_dir,
    write_json,
)
from config.config import CFG, PROJECT_ROOT


@dataclass(frozen=True)
class ShadowFollowupMicroDecision:
    allowed: bool
    reason: str
    failures: tuple[str, ...]
    amount_sol: float
    route_proxy: bool = False
    lane: str = LANE_SHADOW_FOLLOWUP_MICRO


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        out = value
    else:
        raw = str(value or "").replace("Z", "+00:00").strip()
        if not raw:
            return None
        try:
            out = dt.datetime.fromisoformat(raw)
        except Exception:
            return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=dt.timezone.utc)
    return out.astimezone(dt.timezone.utc)


def _age_since_seen_min(row: dict[str, Any]) -> float:
    explicit = _first(row, "minutes_since_first_seen", "shadow_age_min", "age_since_seen_min")
    if explicit is not None:
        return fnum(explicit, 999.0)
    first = _parse_time(_first(row, "first_seen_at", "opened_at"))
    now = _parse_time(_first(row, "ts_utc", "timestamp", "updated_at_utc")) or dt.datetime.now(dt.timezone.utc)
    if first is None:
        return 999.0
    return max(0.0, (now - first).total_seconds() / 60.0)


def _trigger(row: dict[str, Any]) -> str | None:
    shadow_pnl = fnum(_first(row, "shadow_pnl_pct", "pnl_pct", "target_total_pnl_pct"), 0.0)
    age_min = _age_since_seen_min(row)
    partial = fnum(_first(row, "candidate_partial_pnl_pct", "partial_pnl_pct"), 0.0)
    peak = fnum(_first(row, "observed_peak_after_seen", "max_pnl_pct_seen", "shadow_max_pnl_pct_seen", "peak_pnl_pct"), 0.0)
    age_at_seen = fnum(_first(row, "age_at_seen", "age_minutes", "age_min", "token_age_min"), 999.0)
    if shadow_pnl >= 25.0 and age_min <= 3.0:
        return "shadow_pnl_25_within_3m"
    if shadow_pnl >= 50.0 and age_min <= 6.0:
        return "shadow_pnl_50_within_6m"
    if partial >= 50.0:
        return "candidate_partial_50"
    if peak >= 50.0 and age_at_seen <= 10.0:
        return "observed_peak_after_seen_50"
    return None


def evaluate_shadow_followup_micro(
    row: dict[str, Any],
    *,
    open_count: int = 0,
    daily_buys: int = 0,
    dry_run: bool = True,
    live: bool = False,
    cfg: Any = CFG,
) -> ShadowFollowupMicroDecision:
    amount = min(fnum(getattr(cfg, "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL", 0.003), 0.003), 0.003)

    def out(allowed: bool, reason: str, failures: list[str] | tuple[str, ...], *, route_proxy: bool = False) -> ShadowFollowupMicroDecision:
        return ShadowFollowupMicroDecision(bool(allowed), reason, tuple(failures), amount, route_proxy=route_proxy)

    if not bool(getattr(cfg, "SHADOW_FOLLOWUP_MICRO_ENABLED", True)):
        return out(False, "shadow_followup_disabled", ["disabled"])
    if live or not dry_run or not bool(getattr(cfg, "SHADOW_FOLLOWUP_MICRO_PAPER_ENABLED", True)):
        return out(False, "shadow_followup_paper_only", ["paper_only"])
    if bool(getattr(cfg, "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED", False)):
        return out(False, "shadow_followup_live_flag_blocked", ["live_flag_enabled"])
    if open_count >= int(getattr(cfg, "SHADOW_FOLLOWUP_MICRO_MAX_OPEN", 1) or 1):
        return out(False, "shadow_followup_open_cap", ["open_cap"])
    if daily_buys >= int(getattr(cfg, "SHADOW_FOLLOWUP_MICRO_MAX_DAILY_BUYS", 5) or 5):
        return out(False, "shadow_followup_daily_cap", ["daily_cap"])

    failures: list[str] = []
    trigger = _trigger(row)
    if trigger is None:
        failures.append("no_followup_trigger")
    reason_text = " ".join(str(_first(row, key) or "") for key in ("reason", "green_sniper_reason", "reject_reason")).lower()
    if boolish(_first(row, "toxic_initial_sell_pressure", "initial_sell_pressure_toxic"), False) or "toxic_initial_sell_pressure" in reason_text:
        failures.append("toxic_initial_sell_pressure")
    mcap_raw = _first(row, "market_cap_usd", "buy_market_cap_usd", "mcap")
    mcap = fnum(mcap_raw, 0.0)
    if mcap_raw in (None, "") and int(fnum(_first(row, "mcap_missing_ticks", "missing_mcap_ticks"), 0.0)) > 2:
        failures.append("mcap_missing_gt_2_ticks")
    if mcap > 150_000.0:
        failures.append("mcap_gt_150k")
    cluster_bad = boolish(_first(row, "cluster_bad", "helius_cluster_bad"), False) or "cluster_bad" in reason_text
    mode = str(_first(row, "mode", "shadow_followup_mode", "gate_profile") or "").strip().lower()
    if cluster_bad and not (amount <= 0.001 and mode == "moonshot"):
        failures.append("cluster_bad")
    route_ok = boolish(_first(row, "has_jupiter_route", "route_ok", "route_available"), False)
    route_proxy = not route_ok
    if failures:
        return out(False, "shadow_followup_blocked:" + ",".join(failures[:6]), failures, route_proxy=route_proxy)
    return out(True, f"shadow_followup_micro:{trigger}", [], route_proxy=route_proxy)


def apply_shadow_followup_micro_context(row: dict[str, Any], decision: ShadowFollowupMicroDecision) -> dict[str, Any]:
    row["entry_lane"] = decision.lane
    row["gate_profile"] = "shadow_followup_micro"
    row["profit_lane_tier"] = decision.lane
    row["lane_policy_category"] = POLICY_SHADOW_FOLLOWUP_MICRO
    row["green_sniper_reason"] = decision.reason
    row["shadow_followup_micro"] = int(bool(decision.allowed))
    row["shadow_followup_micro_amount_sol"] = float(decision.amount_sol)
    row["shadow_followup_micro_route_proxy"] = int(bool(decision.route_proxy))
    row["route_proxy"] = int(bool(decision.route_proxy))
    row["runner_exit_profile"] = "shadow_followup_micro"
    return row


def build_shadow_followup_micro_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_runtime_events(root) + load_candidate_outcomes(root)
    shadow_rows = [
        row
        for row in rows
        if "shadow" in " ".join(str(_first(row, key) or "") for key in ("sample_type", "reason", "action", "shadow_kind")).lower()
        or _trigger(row) is not None
    ]
    decisions = [evaluate_shadow_followup_micro(row) for row in shadow_rows]
    allowed = [decision for decision in decisions if decision.allowed]
    blocked = collections.Counter(decision.reason for decision in decisions if not decision.allowed)
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "enabled": bool(getattr(CFG, "SHADOW_FOLLOWUP_MICRO_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "SHADOW_FOLLOWUP_MICRO_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED", False)),
            "amount_sol": min(fnum(getattr(CFG, "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL", 0.003), 0.003), 0.003),
            "max_open": int(getattr(CFG, "SHADOW_FOLLOWUP_MICRO_MAX_OPEN", 1) or 1),
            "max_daily_buys": int(getattr(CFG, "SHADOW_FOLLOWUP_MICRO_MAX_DAILY_BUYS", 5) or 5),
        },
        "candidates_seen": len(shadow_rows),
        "micro_triggers": len(allowed),
        "route_proxy": sum(1 for decision in allowed if decision.route_proxy),
        "blocked_by_reason": dict(blocked.most_common()),
        "samples": [
            {
                "address": address_of(row),
                "trigger": _trigger(row),
                "decision": decision.reason,
                "allowed": decision.allowed,
                "route_proxy": decision.route_proxy,
            }
            for row, decision in zip(shadow_rows[:50], decisions[:50])
        ],
    }


def write_shadow_followup_micro_report(root: Path | None = None) -> dict[str, Any]:
    report = build_shadow_followup_micro_report(root)
    write_json(metrics_dir(root) / "shadow_followup_micro_report.json", report)
    return report


__all__ = [
    "ShadowFollowupMicroDecision",
    "apply_shadow_followup_micro_context",
    "build_shadow_followup_micro_report",
    "evaluate_shadow_followup_micro",
    "write_shadow_followup_micro_report",
]
