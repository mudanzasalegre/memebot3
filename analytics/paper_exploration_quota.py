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
    load_deduped_positions,
    load_runtime_events,
    metrics_dir,
    write_json,
)
from config.config import CFG, PROJECT_ROOT
from ml.lane_taxonomy import LANE_MOONSHOT_MICRO_LOTTERY, LANE_PAPER_EXPLORATION_MICRO, LANE_RESEARCH_RANK_CANARY


REPORT_JSON = "paper_exploration_quota_report.json"
POLICY_PAPER_EXPLORATION_QUOTA = "paper_exploration_quota"
ELIGIBLE_LANES = {
    "shadow_followup_micro",
    "moonshot_micro_lottery_confirmed",
    "sniper_research_high_shadow_ev",
    "late_momentum_micro_confirmed",
    "rank_canary_shadow_positive",
}


@dataclass(frozen=True)
class PaperExplorationQuotaDecision:
    allowed: bool
    reason: str
    amount_sol: float
    lane_hint: str
    route_proxy: bool = False
    lane: str = LANE_PAPER_EXPLORATION_MICRO


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _reason(row: dict[str, Any]) -> str:
    return str(
        _first(
            row,
            "reason",
            "green_sniper_reason",
            "entry_reason",
            "reject_reason",
            "sniper_research_subprofile_reason",
        )
        or ""
    ).strip().lower()


def _lane_hint(row: dict[str, Any]) -> str:
    reason = _reason(row)
    gate = str(_first(row, "gate_profile", "sniper_gate_profile", "live_profit_gate_profile") or "").strip().lower()
    lane = str(_first(row, "entry_lane", "lane", "profit_lane_tier") or "").strip().lower()
    subprofile = str(_first(row, "entry_subprofile", "sniper_research_subprofile") or "").strip().lower()
    if "shadow_followup" in reason or lane == "pump_early_shadow_followup_micro":
        return "shadow_followup_micro"
    if lane == LANE_MOONSHOT_MICRO_LOTTERY or "moonshot_micro_lottery" in gate:
        return "moonshot_micro_lottery_confirmed"
    if "confirmed_moonshot_buy" in reason or "moonshot_micro_lottery_confirmed" in reason:
        return "moonshot_micro_lottery_confirmed"
    if (
        "rank_canary_shadow_positive" in reason
        or "rank_canary_shadow_positive" in gate
        or "shadow_rank_canary" in reason
        or (lane == LANE_RESEARCH_RANK_CANARY and boolish(_first(row, "research_rank_canary_shadow"), False))
    ):
        return "rank_canary_shadow_positive"
    if (
        "high_shadow_ev" in reason
        or "shadow_ev" in reason
        or subprofile in {"sniper_research_momentum_ignition", "sniper_research_deep_reversal"}
    ):
        return "sniper_research_high_shadow_ev"
    if "late_momentum_micro_confirmed" in reason:
        return "late_momentum_micro_confirmed"
    return ""


def _route_proxy(row: dict[str, Any]) -> bool:
    return not boolish(_first(row, "has_jupiter_route", "route_ok", "route_available"), False)


def _int_cfg(cfg: Any, name: str, default: int) -> int:
    value = getattr(cfg, name, default)
    if value in (None, ""):
        return int(default)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _float_cfg(cfg: Any, name: str, default: float) -> float:
    value = getattr(cfg, name, default)
    if value in (None, ""):
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _cap_reached(count: int, cap: int) -> bool:
    return cap > 0 and count >= cap


def _blocked(row: dict[str, Any], lane_hint: str, *, cfg: Any = CFG) -> str | None:
    reason = _reason(row)
    if "toxic_initial_sell_pressure" in reason or boolish(_first(row, "toxic_initial_sell_pressure"), False):
        return "toxic_initial_sell_pressure"
    if "cluster_bad" in reason or boolish(_first(row, "cluster_bad", "helius_cluster_bad"), False):
        return "cluster_bad"
    if fnum(_first(row, "price_usd", "buy_price_usd"), 0.0) <= 0.0 and _first(row, "price_pct_5m", "buy_price_pct_5m") is None:
        return "no_price"
    known_proxy_liquidity = boolish(
        _first(row, "liquidity_is_proxy", "liquidity_usd_is_proxy", "buy_liquidity_is_proxy"),
        False,
    )
    if (
        known_proxy_liquidity
        and lane_hint != "moonshot_micro_lottery_confirmed"
        and bool(getattr(cfg, "PAPER_EXPLORATION_BLOCK_PROXY_LIQUIDITY", False))
    ):
        return "known_proxy_liquidity"
    return None


def should_allow_paper_exploration(
    row: dict[str, Any],
    *,
    hours_without_buy: float,
    open_count: int,
    daily_buys: int,
    api_budget_ok: bool = True,
    dry_run: bool = True,
    live: bool = False,
    cfg: Any = CFG,
) -> PaperExplorationQuotaDecision:
    amount = max(
        0.0,
        float(
            getattr(
                cfg,
                "PAPER_IDLE_AMOUNT_SOL",
                getattr(cfg, "PAPER_EXPLORATION_AMOUNT_SOL", 0.1),
            )
            or 0.1
        ),
    )
    lane_hint = _lane_hint(row)
    route_proxy = _route_proxy(row)

    def decision(allowed: bool, reason: str) -> PaperExplorationQuotaDecision:
        return PaperExplorationQuotaDecision(bool(allowed), reason, amount, lane_hint, route_proxy=route_proxy)

    if not bool(getattr(cfg, "PAPER_IDLE_MICRO_EXPLORATION_ENABLED", getattr(cfg, "PAPER_EXPLORATION_QUOTA_ENABLED", True))):
        return decision(False, "paper_exploration_disabled")
    if live or not dry_run:
        return decision(False, "paper_exploration_paper_only")
    if lane_hint not in ELIGIBLE_LANES:
        return decision(False, "paper_exploration_lane_not_allowed")
    if not api_budget_ok:
        return decision(False, "paper_exploration_api_budget_blocked")
    blocker = _blocked(row, lane_hint, cfg=cfg)
    if blocker:
        return decision(False, f"paper_exploration_blocked:{blocker}")
    idle_hours = _float_cfg(cfg, "PAPER_IDLE_AFTER_HOURS", _float_cfg(cfg, "PAPER_EXPLORATION_IDLE_HOURS", 0.0))
    if hours_without_buy < idle_hours:
        return decision(False, "paper_exploration_idle_window_not_met")
    if _cap_reached(open_count, _int_cfg(cfg, "PAPER_EXPLORATION_MAX_OPEN", 0)):
        return decision(False, "paper_exploration_open_cap")
    idle_daily_cap = _int_cfg(
        cfg,
        "PAPER_IDLE_MAX_DAILY_BUYS",
        _int_cfg(cfg, "PAPER_EXPLORATION_MAX_DAILY_BUYS", 0),
    )
    if _cap_reached(daily_buys, idle_daily_cap):
        return decision(False, "paper_exploration_daily_cap")
    return decision(True, POLICY_PAPER_EXPLORATION_QUOTA)


def apply_paper_exploration_quota_context(
    row: dict[str, Any],
    decision: PaperExplorationQuotaDecision,
) -> dict[str, Any]:
    row["entry_lane"] = decision.lane
    row["gate_profile"] = POLICY_PAPER_EXPLORATION_QUOTA
    row["sniper_gate_profile"] = POLICY_PAPER_EXPLORATION_QUOTA
    row["live_profit_gate_profile"] = POLICY_PAPER_EXPLORATION_QUOTA
    row["profit_lane_tier"] = decision.lane
    row["lane_policy_category"] = POLICY_PAPER_EXPLORATION_QUOTA
    row["paper_exploration_quota"] = int(bool(decision.allowed))
    row["paper_exploration_source_lane"] = decision.lane_hint
    row["paper_exploration_quota_amount_sol"] = float(decision.amount_sol)
    row["paper_exploration_quota_route_proxy"] = int(bool(decision.route_proxy))
    row["route_proxy"] = int(bool(decision.route_proxy))
    row["green_sniper_reason"] = POLICY_PAPER_EXPLORATION_QUOTA
    row["entry_reason"] = POLICY_PAPER_EXPLORATION_QUOTA
    row["sniper_research_subprofile_reason"] = POLICY_PAPER_EXPLORATION_QUOTA
    row["live_profit_gate_failed_count"] = 0
    row["live_profit_gate_failures"] = ""
    row["runner_exit_profile"] = POLICY_PAPER_EXPLORATION_QUOTA
    return row


def build_paper_exploration_quota_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_runtime_events(root) + load_candidate_outcomes(root) + load_deduped_positions(root)
    eligible = [row for row in rows if _lane_hint(row) in ELIGIBLE_LANES]
    buys = [
        row
        for row in rows
        if "paper_exploration_quota" in _reason(row)
        or boolish(row.get("paper_exploration_quota"), False)
        or str(_first(row, "entry_lane", "lane", "profit_lane_tier") or "").strip().lower() == LANE_PAPER_EXPLORATION_MICRO
    ]
    blocked: dict[str, int] = {}
    for row in eligible:
        blocker = _blocked(row, _lane_hint(row), cfg=CFG)
        if blocker:
            blocked[blocker] = blocked.get(blocker, 0) + 1
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "enabled": bool(getattr(CFG, "PAPER_IDLE_MICRO_EXPLORATION_ENABLED", getattr(CFG, "PAPER_EXPLORATION_QUOTA_ENABLED", True))),
            "amount_sol": max(
                0.0,
                float(getattr(CFG, "PAPER_IDLE_AMOUNT_SOL", getattr(CFG, "PAPER_EXPLORATION_AMOUNT_SOL", 0.1)) or 0.1),
            ),
            "max_open": _int_cfg(CFG, "PAPER_EXPLORATION_MAX_OPEN", 0),
            "max_daily_buys": _int_cfg(
                CFG,
                "PAPER_IDLE_MAX_DAILY_BUYS",
                _int_cfg(CFG, "PAPER_EXPLORATION_MAX_DAILY_BUYS", 0),
            ),
            "idle_hours": _float_cfg(CFG, "PAPER_IDLE_AFTER_HOURS", _float_cfg(CFG, "PAPER_EXPLORATION_IDLE_HOURS", 0.0)),
            "eligible_lanes": sorted(ELIGIBLE_LANES),
            "block_proxy_liquidity": bool(getattr(CFG, "PAPER_EXPLORATION_BLOCK_PROXY_LIQUIDITY", False)),
        },
        "eligible_shadows": len(eligible),
        "quota_buys": len(buys),
        "blocked": dict(sorted(blocked.items())),
        "samples": [
            {
                "address": address_of(row),
                "lane_hint": _lane_hint(row),
                "reason": _reason(row),
                "route_proxy": _route_proxy(row),
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
    "POLICY_PAPER_EXPLORATION_QUOTA",
    "PaperExplorationQuotaDecision",
    "apply_paper_exploration_quota_context",
    "build_paper_exploration_quota_report",
    "should_allow_paper_exploration",
    "write_paper_exploration_quota_report",
]
