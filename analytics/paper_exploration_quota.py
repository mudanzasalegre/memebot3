from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from config.config import CFG, PROJECT_ROOT


REPORT_JSON = "paper_exploration_quota_report.json"
ELIGIBLE_LANES = {
    "shadow_followup_micro",
    "moonshot_micro_lottery_confirmed",
    "sniper_research_high_shadow_ev",
    "late_momentum_micro_confirmed",
}


@dataclass(frozen=True)
class PaperExplorationQuotaDecision:
    allowed: bool
    reason: str
    amount_sol: float
    lane_hint: str


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _reason(row: dict[str, Any]) -> str:
    return str(_first(row, "reason", "green_sniper_reason", "sniper_research_subprofile_reason") or "").strip().lower()


def _lane_hint(row: dict[str, Any]) -> str:
    reason = _reason(row)
    lane = str(_first(row, "entry_lane", "lane", "profit_lane_tier") or "").strip().lower()
    subprofile = str(_first(row, "entry_subprofile", "sniper_research_subprofile") or "").strip().lower()
    if "shadow_followup" in reason or lane == "pump_early_shadow_followup_micro":
        return "shadow_followup_micro"
    if "confirmed_moonshot_buy" in reason or "moonshot_micro_lottery_confirmed" in reason:
        return "moonshot_micro_lottery_confirmed"
    if (
        "high_shadow_ev" in reason
        or "shadow_ev" in reason
        or subprofile in {"sniper_research_momentum_ignition", "sniper_research_deep_reversal"}
    ):
        return "sniper_research_high_shadow_ev"
    if "late_momentum_micro_confirmed" in reason:
        return "late_momentum_micro_confirmed"
    return ""


def _blocked(row: dict[str, Any], lane_hint: str) -> str | None:
    reason = _reason(row)
    if "toxic_initial_sell_pressure" in reason or boolish(_first(row, "toxic_initial_sell_pressure"), False):
        return "toxic_initial_sell_pressure"
    if boolish(_first(row, "cluster_bad", "helius_cluster_bad"), False):
        return "cluster_bad"
    if fnum(_first(row, "price_usd", "buy_price_usd"), 0.0) <= 0.0 and _first(row, "price_pct_5m", "buy_price_pct_5m") is None:
        return "no_price"
    route_ok = boolish(_first(row, "has_jupiter_route", "route_ok", "route_available"), False)
    if not route_ok and lane_hint != "moonshot_micro_lottery_confirmed":
        return "no_route"
    return None


def should_allow_paper_exploration(
    row: dict[str, Any],
    *,
    hours_without_buy: float,
    open_count: int,
    daily_buys: int,
    cfg: Any = CFG,
) -> PaperExplorationQuotaDecision:
    amount = min(
        float(
            getattr(
                cfg,
                "PAPER_IDLE_AMOUNT_SOL",
                getattr(cfg, "PAPER_EXPLORATION_AMOUNT_SOL", 0.002),
            )
            or 0.002
        ),
        0.002,
    )
    lane_hint = _lane_hint(row)
    if not bool(getattr(cfg, "PAPER_IDLE_MICRO_EXPLORATION_ENABLED", getattr(cfg, "PAPER_EXPLORATION_QUOTA_ENABLED", True))):
        return PaperExplorationQuotaDecision(False, "paper_exploration_disabled", amount, lane_hint)
    if lane_hint not in ELIGIBLE_LANES:
        return PaperExplorationQuotaDecision(False, "paper_exploration_lane_not_allowed", amount, lane_hint)
    blocker = _blocked(row, lane_hint)
    if blocker:
        return PaperExplorationQuotaDecision(False, f"paper_exploration_blocked:{blocker}", amount, lane_hint)
    if hours_without_buy < float(getattr(cfg, "PAPER_IDLE_AFTER_HOURS", getattr(cfg, "PAPER_EXPLORATION_IDLE_HOURS", 3.0)) or 3.0):
        return PaperExplorationQuotaDecision(False, "paper_exploration_idle_window_not_met", amount, lane_hint)
    if open_count >= int(getattr(cfg, "PAPER_EXPLORATION_MAX_OPEN", 1) or 1):
        return PaperExplorationQuotaDecision(False, "paper_exploration_open_cap", amount, lane_hint)
    if daily_buys >= int(getattr(cfg, "PAPER_IDLE_MAX_DAILY_BUYS", getattr(cfg, "PAPER_EXPLORATION_MAX_DAILY_BUYS", 3)) or 3):
        return PaperExplorationQuotaDecision(False, "paper_exploration_daily_cap", amount, lane_hint)
    return PaperExplorationQuotaDecision(True, "paper_idle_micro_exploration", amount, lane_hint)


def build_paper_exploration_quota_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_runtime_events(root) + load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    eligible = [row for row in rows if _lane_hint(row) in ELIGIBLE_LANES]
    buys = [
        row
        for row in rows
        if "paper_exploration_quota" in _reason(row)
        or boolish(row.get("paper_exploration_quota"), False)
    ]
    blocked: dict[str, int] = {}
    for row in eligible:
        blocker = _blocked(row, _lane_hint(row))
        if blocker:
            blocked[blocker] = blocked.get(blocker, 0) + 1
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "enabled": bool(getattr(CFG, "PAPER_IDLE_MICRO_EXPLORATION_ENABLED", getattr(CFG, "PAPER_EXPLORATION_QUOTA_ENABLED", True))),
            "amount_sol": min(float(getattr(CFG, "PAPER_IDLE_AMOUNT_SOL", getattr(CFG, "PAPER_EXPLORATION_AMOUNT_SOL", 0.002)) or 0.002), 0.002),
            "max_open": int(getattr(CFG, "PAPER_EXPLORATION_MAX_OPEN", 1) or 1),
            "max_daily_buys": int(getattr(CFG, "PAPER_IDLE_MAX_DAILY_BUYS", getattr(CFG, "PAPER_EXPLORATION_MAX_DAILY_BUYS", 3)) or 3),
            "idle_hours": float(getattr(CFG, "PAPER_IDLE_AFTER_HOURS", getattr(CFG, "PAPER_EXPLORATION_IDLE_HOURS", 3.0)) or 3.0),
            "eligible_lanes": sorted(ELIGIBLE_LANES),
        },
        "eligible_shadows": len(eligible),
        "quota_buys": len(buys),
        "blocked": dict(sorted(blocked.items())),
        "samples": [
            {
                "address": address_of(row),
                "lane_hint": _lane_hint(row),
                "reason": _reason(row),
            }
            for row in eligible[:50]
        ],
    }


def write_paper_exploration_quota_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_paper_exploration_quota_report(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    return report


__all__ = [
    "ELIGIBLE_LANES",
    "PaperExplorationQuotaDecision",
    "build_paper_exploration_quota_report",
    "should_allow_paper_exploration",
    "write_paper_exploration_quota_report",
]
