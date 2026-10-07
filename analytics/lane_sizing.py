from __future__ import annotations

import collections
import datetime as dt
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analytics.report_utils import (
    fnum,
    load_candidate_outcomes,
    load_deduped_positions,
    load_runtime_events,
    metrics_dir,
    write_json,
)
from config.config import CFG, PROJECT_ROOT
from ml.lane_taxonomy import (
    LANE_BIRTH_PROBE_MICRO_CANARY,
    LANE_MOONSHOT_MICRO_LOTTERY,
    LANE_PAPER_BOOTSTRAP_MICRO,
    LANE_PAPER_EXPLORATION_MICRO,
    LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH,
    LANE_RESEARCH_RANK_CANARY,
    LANE_RESEARCH_SNIPER,
    LANE_SHADOW_FOLLOWUP_MICRO,
    LANE_SNIPER_RESEARCH_MICRO_FALLBACK,
    normalize_entry_lane,
)

EXPERIMENTAL_MAX_SOL = 0.03
WARNING_SAMPLE_LIMIT = 100
MICRO_LANES = {
    LANE_BIRTH_PROBE_MICRO_CANARY,
    LANE_MOONSHOT_MICRO_LOTTERY,
    LANE_PAPER_BOOTSTRAP_MICRO,
    LANE_PAPER_EXPLORATION_MICRO,
    LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH,
    LANE_SHADOW_FOLLOWUP_MICRO,
    LANE_SNIPER_RESEARCH_MICRO_FALLBACK,
}
MICRO_HARD_CAP_EXEMPT_LANES = {
    LANE_PAPER_BOOTSTRAP_MICRO,
    LANE_PAPER_EXPLORATION_MICRO,
}


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
    if "paper_exploration" in reason:
        return LANE_PAPER_EXPLORATION_MICRO
    if "paper_bootstrap" in reason:
        return LANE_PAPER_BOOTSTRAP_MICRO
    if "sniper_research_micro_fallback" in reason:
        return LANE_SNIPER_RESEARCH_MICRO_FALLBACK
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


def _cfg_bool(cfg: Any, name: str, default: bool) -> bool:
    value = getattr(cfg, name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _is_micro_lane(lane: Any) -> bool:
    lane_key = normalize_entry_lane(lane)
    return lane_key in MICRO_LANES or lane_key.endswith("_micro") or "micro" in lane_key


def _uses_micro_hard_cap(lane: Any) -> bool:
    lane_key = normalize_entry_lane(lane)
    return _is_micro_lane(lane_key) and lane_key not in MICRO_HARD_CAP_EXEMPT_LANES


def _lane_cap(
    lane: str,
    lane_amount: float,
    *,
    dry_run: bool,
    live: bool,
    cfg: Any,
) -> float:
    amount = max(0.0, float(lane_amount or 0.0))
    cap = amount
    if lane == LANE_RESEARCH_RANK_CANARY:
        cap = min(cap, _cfg_float(cfg, "RESEARCH_RANK_CANARY_MAX_SIZE_SOL", max(amount, EXPERIMENTAL_MAX_SOL)))
    if lane == LANE_PAPER_BOOTSTRAP_MICRO:
        cap = min(cap, _cfg_float(cfg, "PAPER_BOOTSTRAP_MAX_AMOUNT_SOL", amount))
    if dry_run and not live:
        max_paper_trade = _cfg_float(cfg, "PAPER_MAX_TRADE_AMOUNT_SOL", _cfg_float(cfg, "MAX_TRADE_AMOUNT_SOL", cap))
        if max_paper_trade > 0:
            cap = min(cap, max_paper_trade)
    if not dry_run and live:
        max_trade = _cfg_float(cfg, "MAX_TRADE_AMOUNT_SOL", cap)
        if max_trade > 0:
            cap = min(cap, max_trade)
    return cap


def _lane_amount(row: dict[str, Any], lane: str, *, cfg: Any) -> tuple[float, str]:
    default_amount = _cfg_float(cfg, "DEFAULT_PAPER_BUY_SOL", 0.1)
    if lane == LANE_RESEARCH_RANK_CANARY:
        reason_text = str(_first(row, "reason", "green_sniper_reason", "entry_reason") or "").lower()
        if "paper_normal" in reason_text:
            return _cfg_float(cfg, "RESEARCH_RANK_CANARY_PAPER_NORMAL_SIZE_SOL", 0.005), "rank_paper_normal_size"
        if reason_text.find("priority") >= 0:
            return _cfg_float(cfg, "RESEARCH_RANK_CANARY_PRIORITY_SIZE_SOL", 0.02), "rank_priority_size"
        return _cfg_float(cfg, "RESEARCH_RANK_CANARY_SIZE_SOL", 0.005), "rank_canary_size"
    if lane == LANE_RESEARCH_SNIPER:
        sub = _subprofile(row)
        if "momentum" in sub:
            return _cfg_float(cfg, "SNIPER_RESEARCH_MOMENTUM_SIZE_SOL", 0.005), "sniper_momentum_size"
        if "deep_reversal" in sub:
            return _cfg_float(cfg, "SNIPER_RESEARCH_DEEP_REVERSAL_SIZE_SOL", 0.005), "sniper_deep_reversal_size"
        return _cfg_float(cfg, "SNIPER_RESEARCH_SIZE_SOL", 0.005), "sniper_research_size"
    if lane == LANE_SNIPER_RESEARCH_MICRO_FALLBACK:
        return _cfg_float(cfg, "SNIPER_RESEARCH_MICRO_FALLBACK_AMOUNT_SOL", 0.003), "sniper_research_micro_fallback_size"
    if lane == LANE_MOONSHOT_MICRO_LOTTERY:
        return _cfg_float(cfg, "MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL", 0.001), "moonshot_micro_size"
    if lane == LANE_SHADOW_FOLLOWUP_MICRO:
        return _cfg_float(cfg, "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL", 0.003), "shadow_followup_micro_size"
    if lane == LANE_PAPER_EXPLORATION_MICRO:
        return _cfg_float(cfg, "PAPER_IDLE_AMOUNT_SOL", _cfg_float(cfg, "PAPER_EXPLORATION_AMOUNT_SOL", 0.1)), "paper_exploration_micro_size"
    if lane == LANE_PAPER_BOOTSTRAP_MICRO:
        return _cfg_float(cfg, "PAPER_BOOTSTRAP_AMOUNT_SOL", 0.1), "paper_bootstrap_micro_size"
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
    try:
        input_amount = float(computed_amount_sol)
    except (TypeError, ValueError, OverflowError):
        input_amount = 0.0
    if not math.isfinite(input_amount) or input_amount <= 0:
        return LaneSizingDecision(0.0, lane, "invalid_computed_trade_amount", 0.0, 0.0)
    if dry_run and not live and _cfg_bool(cfg, "PAPER_EXACT_TRADE_SIZE_ENABLED", False):
        try:
            requested = float(getattr(cfg, "PAPER_EXACT_TRADE_SIZE_SOL", 0.1))
            cap = float(getattr(cfg, "PAPER_MAX_TRADE_AMOUNT_SOL", requested))
        except (TypeError, ValueError, OverflowError):
            requested, cap = 0.0, 0.0
        if (not math.isfinite(requested) or requested <= 0 or not math.isfinite(cap)
                or cap < 0 or (cap > 0 and cap + 1e-9 < requested)):
            return LaneSizingDecision(0.0, lane, "exact_paper_size_invalid_or_exceeds_cap", input_amount, cap if math.isfinite(cap) else 0.0,
                                      fallback_blocked=True)
        return LaneSizingDecision(requested, lane, "exact_paper_trade_amount", input_amount, cap)
    if not bool(getattr(cfg, "LANE_SIZING_ENABLED", True)):
        return LaneSizingDecision(input_amount, lane, "lane_sizing_disabled", input_amount, input_amount)
    if _cfg_bool(cfg, "LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED", True):
        allowlist = _csv(getattr(cfg, "LANE_SIZING_TRADE_AMOUNT_ALLOWLIST", ""))
        lane_key = str(lane or "").lower()
        fixed_allowed_for_lane = not _is_micro_lane(lane_key) or lane_key in allowlist
    else:
        allowlist = _csv(getattr(cfg, "LANE_SIZING_TRADE_AMOUNT_ALLOWLIST", ""))
        lane_key = str(lane or "").lower()
        fixed_allowed_for_lane = False

    if fixed_allowed_for_lane:
        if dry_run and not live:
            trade_amount = _cfg_float(
                cfg,
                "TRADE_AMOUNT_SOL",
                _cfg_float(cfg, "DEFAULT_PAPER_BUY_SOL", input_amount),
            )
            cap = _cfg_float(cfg, "PAPER_MAX_TRADE_AMOUNT_SOL", _cfg_float(cfg, "MAX_TRADE_AMOUNT_SOL", trade_amount))
            amount = trade_amount if input_amount > 0.0 else 0.0
            if cap > 0.0:
                amount = min(amount, cap)
            return LaneSizingDecision(
                amount,
                lane,
                "fixed_paper_trade_amount",
                input_amount,
                cap,
            )
        if live and not dry_run:
            cap = _cfg_float(cfg, "MAX_TRADE_AMOUNT_SOL", _cfg_float(cfg, "TRADE_AMOUNT_SOL", input_amount))
            amount = input_amount
            if cap > 0.0:
                amount = min(amount, cap)
            return LaneSizingDecision(
                amount,
                lane,
                "fixed_live_trade_amount",
                input_amount,
                cap,
            )
    if lane_key in allowlist:
        cap_name = "PAPER_MAX_TRADE_AMOUNT_SOL" if dry_run and not live else "MAX_TRADE_AMOUNT_SOL"
        cap = _cfg_float(cfg, cap_name, _cfg_float(cfg, "MAX_TRADE_AMOUNT_SOL", input_amount))
        amount = min(input_amount, cap) if cap > 0 else input_amount
        return LaneSizingDecision(amount, lane, "trade_amount_allowlisted", input_amount, cap)
    lane_amount, reason = _lane_amount(row, lane, cfg=cfg)
    cap = _lane_cap(
        lane,
        lane_amount,
        dry_run=dry_run,
        live=live,
        cfg=cfg,
    )
    if lane == LANE_RESEARCH_RANK_CANARY and reason == "rank_paper_normal_size" and input_amount > 0.0:
        lane_amount = min(lane_amount, input_amount)
    amount = max(0.0, min(float(lane_amount or 0.0), float(cap or 0.0)))
    fallback_blocked = input_amount > amount and input_amount >= 0.099
    warning = (
        "experimental_lane_over_0.03"
        if amount > EXPERIMENTAL_MAX_SOL
        and lane not in {LANE_PAPER_BOOTSTRAP_MICRO, LANE_PAPER_EXPLORATION_MICRO}
        else ""
    )
    micro_hard_cap = _cfg_float(cfg, "MICRO_LANE_HARD_CAP_SOL", 0.01)
    if _uses_micro_hard_cap(lane_key) and micro_hard_cap > 0.0 and amount > micro_hard_cap:
        cap = min(cap, micro_hard_cap) if cap > 0 else micro_hard_cap
        amount = min(amount, micro_hard_cap)
        warning = "micro_lane_hard_cap"
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
    rows = load_runtime_events(root) + load_candidate_outcomes(root) + load_deduped_positions(root)
    grouped: dict[str, list[float]] = collections.defaultdict(list)
    fallback_blocked = 0
    warnings: list[dict[str, Any]] = []
    warning_counts: collections.Counter[tuple[str, str]] = collections.Counter()
    for row in rows:
        observed = fnum(_first(row, "buy_amount_sol", "amount_sol", "trade_amount_sol"), 0.0)
        if observed <= 0:
            observed = fnum(getattr(CFG, "TRADE_AMOUNT_SOL", 0.1), 0.1)
        decision = resolve_lane_buy_amount(row, computed_amount_sol=observed, dry_run=True, live=False)
        grouped[decision.lane].append(decision.amount_sol)
        fallback_blocked += int(decision.fallback_blocked)
        if decision.warning:
            warning_counts[(decision.lane, decision.warning)] += 1
            if len(warnings) < WARNING_SAMPLE_LIMIT:
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
            "fixed_trade_amount_enabled": _cfg_bool(CFG, "LANE_SIZING_FIXED_TRADE_AMOUNT_ENABLED", True),
            "default_paper_buy_sol": _cfg_float(CFG, "DEFAULT_PAPER_BUY_SOL", 0.1),
            "trade_amount_sol": _cfg_float(CFG, "TRADE_AMOUNT_SOL", 0.1),
            "paper_max_trade_amount_sol": _cfg_float(CFG, "PAPER_MAX_TRADE_AMOUNT_SOL", 0.1),
            "max_trade_amount_sol": _cfg_float(CFG, "MAX_TRADE_AMOUNT_SOL", 0.1),
            "trade_amount_allowlist": sorted(_csv(getattr(CFG, "LANE_SIZING_TRADE_AMOUNT_ALLOWLIST", ""))),
            "micro_lane_hard_cap_sol": _cfg_float(CFG, "MICRO_LANE_HARD_CAP_SOL", 0.01),
            "experimental_max_sol": EXPERIMENTAL_MAX_SOL,
        },
        "lanes": lanes,
        "fallback_trade_amount_blocked": fallback_blocked,
        "warning_counts": [
            {"lane": lane, "warning": warning, "count": int(count)}
            for (lane, warning), count in warning_counts.most_common()
        ],
        "warnings": warnings,
        "warnings_truncated": max(0, sum(warning_counts.values()) - len(warnings)),
    }


def write_lane_sizing_report(root: Path | None = None) -> dict[str, Any]:
    report = build_lane_sizing_report(root)
    write_json(metrics_dir(root) / "lane_sizing_report.json", report)
    return report


__all__ = [
    "EXPERIMENTAL_MAX_SOL",
    "WARNING_SAMPLE_LIMIT",
    "LANE_SHADOW_FOLLOWUP_MICRO",
    "LANE_SNIPER_RESEARCH_MICRO_FALLBACK",
    "LaneSizingDecision",
    "build_lane_sizing_report",
    "resolve_lane_buy_amount",
    "write_lane_sizing_report",
]
