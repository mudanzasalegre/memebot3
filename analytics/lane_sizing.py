from __future__ import annotations

import collections
import datetime as dt
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analytics.report_utils import (
    fnum,
    load_candidate_outcomes,
    load_paper_positions,
    load_runtime_events,
    load_sqlite_positions,
    metrics_dir,
    write_json,
)
from config.config import CFG, PROJECT_ROOT
from ml.lane_taxonomy import (
    LANE_BIRTH_PROBE_MICRO_CANARY,
    LANE_MOONSHOT_MICRO_LOTTERY,
    LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH,
    LANE_RESEARCH_RANK_CANARY,
    LANE_RESEARCH_SNIPER,
    normalize_entry_lane,
)

LANE_SHADOW_FOLLOWUP_MICRO = "pump_early_shadow_followup_micro"
EXPERIMENTAL_MAX_SOL = 0.03


@dataclass(frozen=True)
class LaneSizingDecision:
    amount_sol: float
    lane: str
    reason: str
    input_amount_sol: float
    cap_sol: float
    fallback_blocked: bool = False
    warning: str = ""


def _csv(value: Any) -> set[str]:
    return {item.strip().lower() for item in str(value or "").split(",") if item.strip()}


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _lane(row: dict[str, Any]) -> str:
    lane = normalize_entry_lane(_first(row, "entry_lane", "lane", "profit_lane_tier"))
    if lane != "unknown":
        return lane
    reason = str(_first(row, "reason", "green_sniper_reason", "gate_profile", "entry_subprofile") or "").lower()
    if "shadow_followup" in reason:
        return LANE_SHADOW_FOLLOWUP_MICRO
    if "moonshot_micro_lottery" in reason:
        return LANE_MOONSHOT_MICRO_LOTTERY
    if "late_momentum" in reason:
        return LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH
    if "research_rank_canary" in reason:
        return LANE_RESEARCH_RANK_CANARY
    if "sniper_research" in reason:
        return LANE_RESEARCH_SNIPER
    return lane


def _subprofile(row: dict[str, Any]) -> str:
    return str(_first(row, "entry_subprofile", "sniper_research_subprofile", "gate_profile") or "").strip().lower()


def _cfg_float(cfg: Any, name: str, default: float) -> float:
    return fnum(getattr(cfg, name, default), default)


def _lane_amount(row: dict[str, Any], lane: str, *, cfg: Any) -> tuple[float, str]:
    default_amount = _cfg_float(cfg, "DEFAULT_PAPER_BUY_SOL", 0.005)
    if lane == LANE_RESEARCH_RANK_CANARY:
        reason_text = str(_first(row, "reason", "green_sniper_reason", "entry_reason") or "").lower()
        if "paper_normal" in reason_text:
            return _cfg_float(cfg, "RESEARCH_RANK_CANARY_PAPER_NORMAL_SIZE_SOL", 0.005), "rank_paper_normal_size"
        if reason_text.find("priority") >= 0:
            return _cfg_float(cfg, "RESEARCH_RANK_CANARY_PRIORITY_SIZE_SOL", 0.02), "rank_priority_size"
        return _cfg_float(cfg, "RESEARCH_RANK_CANARY_SIZE_SOL", 0.02), "rank_canary_size"
    if lane == LANE_RESEARCH_SNIPER:
        sub = _subprofile(row)
        if "momentum" in sub:
            return _cfg_float(cfg, "SNIPER_RESEARCH_MOMENTUM_SIZE_SOL", 0.005), "sniper_momentum_size"
        if "deep_reversal" in sub:
            return _cfg_float(cfg, "SNIPER_RESEARCH_DEEP_REVERSAL_SIZE_SOL", 0.005), "sniper_deep_reversal_size"
        return _cfg_float(cfg, "SNIPER_RESEARCH_SIZE_SOL", 0.005), "sniper_research_size"
    if lane == LANE_MOONSHOT_MICRO_LOTTERY:
        return _cfg_float(cfg, "MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL", 0.001), "moonshot_micro_size"
    if lane == LANE_SHADOW_FOLLOWUP_MICRO:
        return _cfg_float(cfg, "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL", 0.003), "shadow_followup_micro_size"
    if lane == LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH:
        return _cfg_float(cfg, "LATE_MOMENTUM_MICRO_AMOUNT_SOL", 0.003), "late_momentum_micro_size"
    if lane == LANE_BIRTH_PROBE_MICRO_CANARY:
        return _cfg_float(cfg, "BIRTH_PROBE_MICRO_CANARY_AMOUNT_SOL", default_amount), "birth_probe_micro_size"
    return default_amount, "default_paper_buy_size"


def resolve_lane_buy_amount(
    row: dict[str, Any],
    *,
    computed_amount_sol: float,
    dry_run: bool,
    live: bool,
    cfg: Any = CFG,
) -> LaneSizingDecision:
    lane = _lane(row)
    input_amount = max(0.0, float(computed_amount_sol or 0.0))
    if not bool(getattr(cfg, "LANE_SIZING_ENABLED", True)):
        return LaneSizingDecision(input_amount, lane, "lane_sizing_disabled", input_amount, input_amount)
    allowlist = _csv(getattr(cfg, "LANE_SIZING_TRADE_AMOUNT_ALLOWLIST", ""))
    lane_key = str(lane or "").lower()
    if lane_key in allowlist:
        cap = _cfg_float(cfg, "MAX_TRADE_AMOUNT_SOL", input_amount)
        amount = min(input_amount, cap) if cap > 0 else input_amount
        return LaneSizingDecision(amount, lane, "trade_amount_allowlisted", input_amount, cap)
    lane_amount, reason = _lane_amount(row, lane, cfg=cfg)
    cap = _cfg_float(cfg, "RESEARCH_RANK_CANARY_MAX_SIZE_SOL", EXPERIMENTAL_MAX_SOL) if lane == LANE_RESEARCH_RANK_CANARY else EXPERIMENTAL_MAX_SOL
    if lane == LANE_MOONSHOT_MICRO_LOTTERY:
        cap = min(cap, 0.001)
    if lane == LANE_SHADOW_FOLLOWUP_MICRO:
        cap = min(cap, 0.003)
    if lane == LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH:
        cap = min(cap, 0.003)
    if lane == LANE_RESEARCH_RANK_CANARY and reason == "rank_paper_normal_size" and input_amount > 0.0:
        lane_amount = min(lane_amount, input_amount)
    if not dry_run and live:
        cap = min(cap, _cfg_float(cfg, "MAX_TRADE_AMOUNT_SOL", cap))
    amount = max(0.0, min(float(lane_amount or 0.0), float(cap or 0.0)))
    fallback_blocked = input_amount > amount and input_amount >= 0.099
    warning = "experimental_lane_over_0.03" if amount > EXPERIMENTAL_MAX_SOL else ""
    return LaneSizingDecision(
        amount_sol=amount,
        lane=lane,
        reason=reason,
        input_amount_sol=input_amount,
        cap_sol=cap,
        fallback_blocked=fallback_blocked,
        warning=warning,
    )


def build_lane_sizing_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_runtime_events(root) + load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    grouped: dict[str, list[float]] = collections.defaultdict(list)
    fallback_blocked = 0
    warnings: list[dict[str, Any]] = []
    for row in rows:
        observed = fnum(_first(row, "buy_amount_sol", "amount_sol", "trade_amount_sol"), 0.0)
        if observed <= 0:
            observed = fnum(getattr(CFG, "TRADE_AMOUNT_SOL", 0.1), 0.1)
        decision = resolve_lane_buy_amount(row, computed_amount_sol=observed, dry_run=True, live=False)
        grouped[decision.lane].append(decision.amount_sol)
        fallback_blocked += int(decision.fallback_blocked)
        if decision.warning:
            warnings.append(
                {
                    "lane": decision.lane,
                    "amount_sol": decision.amount_sol,
                    "warning": decision.warning,
                    "reason": decision.reason,
                }
            )
    lanes = {
        lane: {
            "rows": len(values),
            "amount_min_sol": round(min(values), 6) if values else 0.0,
            "amount_max_sol": round(max(values), 6) if values else 0.0,
            "amount_avg_sol": round(sum(values) / len(values), 6) if values else 0.0,
            "amount_median_sol": round(statistics.median(values), 6) if values else 0.0,
        }
        for lane, values in sorted(grouped.items())
    }
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "lane_sizing_enabled": bool(getattr(CFG, "LANE_SIZING_ENABLED", True)),
            "default_paper_buy_sol": _cfg_float(CFG, "DEFAULT_PAPER_BUY_SOL", 0.005),
            "trade_amount_allowlist": sorted(_csv(getattr(CFG, "LANE_SIZING_TRADE_AMOUNT_ALLOWLIST", ""))),
            "experimental_max_sol": EXPERIMENTAL_MAX_SOL,
        },
        "lanes": lanes,
        "fallback_trade_amount_blocked": fallback_blocked,
        "warnings": warnings,
    }


def write_lane_sizing_report(root: Path | None = None) -> dict[str, Any]:
    report = build_lane_sizing_report(root)
    write_json(metrics_dir(root) / "lane_sizing_report.json", report)
    return report


__all__ = [
    "EXPERIMENTAL_MAX_SOL",
    "LANE_SHADOW_FOLLOWUP_MICRO",
    "LaneSizingDecision",
    "build_lane_sizing_report",
    "resolve_lane_buy_amount",
    "write_lane_sizing_report",
]
