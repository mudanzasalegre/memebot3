from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from config.config import CFG
from ml.lane_taxonomy import (
    LANE_BIRTH_PROBE_MICRO_CANARY,
    LANE_MOONSHOT_MICRO_LOTTERY,
    LANE_PUMP_EARLY_BREAKOUT,
    LANE_PUMP_EARLY_GREEN_SNIPER,
    LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH,
    LANE_PUMP_EARLY_PROFIT,
    LANE_PUMPSWAP_REBOUND_PRIME,
    LANE_RESEARCH_RANK_CANARY,
    LANE_RESEARCH_SNIPER,
    LANE_SHADOW_FOLLOWUP_MICRO,
    normalize_entry_lane,
)


def _int_cfg(name: str, default: int) -> int:
    value = getattr(CFG, name, None)
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except Exception:
        return int(default)


@dataclass(frozen=True)
class PositionLimitDecision:
    allowed: bool
    lane: str
    open_count: int
    cap: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cap_for_lane(lane: str, *, dry_run: bool, live: bool) -> int:
    lane = normalize_entry_lane(lane)
    if lane == LANE_PUMP_EARLY_GREEN_SNIPER:
        if live:
            return _int_cfg("GREEN_SNIPER_LIVE_MAX_OPEN", 1)
        return _int_cfg("GREEN_SNIPER_MAX_OPEN_PAPER", _int_cfg("PUMP_EARLY_SNIPER_MAX_OPEN_PAPER", 6))
    if lane == LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH:
        if live:
            return _int_cfg("LATE_MOMENTUM_WATCH_MAX_OPEN_LIVE", 0)
        return _int_cfg("LATE_MOMENTUM_WATCH_MAX_OPEN_PAPER", 1)
    if lane == LANE_BIRTH_PROBE_MICRO_CANARY:
        if live:
            return 0
        return _int_cfg("BIRTH_PROBE_MICRO_CANARY_MAX_OPEN", 1)
    if lane == LANE_MOONSHOT_MICRO_LOTTERY:
        if live:
            return 0
        return _int_cfg("MOONSHOT_MICRO_LOTTERY_MAX_OPEN", 1)
    if lane == LANE_SHADOW_FOLLOWUP_MICRO:
        if live:
            return 0
        return _int_cfg("SHADOW_FOLLOWUP_MICRO_MAX_OPEN", 1)
    if lane == LANE_PUMP_EARLY_BREAKOUT:
        return _int_cfg("PUMP_EARLY_BREAKOUT_MAX_OPEN_LIVE_CANARY" if live else "PUMP_EARLY_BREAKOUT_MAX_OPEN_PAPER", 1)
    if lane == LANE_PUMPSWAP_REBOUND_PRIME:
        return _int_cfg("PUMP_EARLY_PROFIT_MAX_OPEN_LIVE_CANARY" if live else "PUMP_EARLY_PROFIT_MAX_OPEN_PAPER", 2)
    if lane == LANE_PUMP_EARLY_PROFIT:
        return _int_cfg("PUMP_EARLY_PROFIT_MAX_OPEN_LIVE_CANARY" if live else "PUMP_EARLY_PROFIT_MAX_OPEN_PAPER", 2)
    if lane == LANE_RESEARCH_SNIPER:
        return _int_cfg("RESEARCH_SHADOW_MAX_OPEN_PER_REGIME", 4)
    if lane == LANE_RESEARCH_RANK_CANARY:
        return _int_cfg("RESEARCH_RANK_CANARY_PRIORITY_MAX_OPEN", _int_cfg("RESEARCH_RANK_CANARY_MAX_OPEN", 1))
    return 999 if dry_run else 1


def count_open_by_lane(open_positions: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pos in open_positions:
        lane = normalize_entry_lane(getattr(pos, "entry_lane", None) if not isinstance(pos, dict) else pos.get("entry_lane"))
        counts[lane] = counts.get(lane, 0) + 1
    return counts


def evaluate_lane_position_limit(
    lane: str,
    open_positions: Iterable[Any],
    *,
    dry_run: bool,
    live: bool,
) -> PositionLimitDecision:
    normalized = normalize_entry_lane(lane)
    counts = count_open_by_lane(open_positions)
    open_count = int(counts.get(normalized, 0))
    cap = _cap_for_lane(normalized, dry_run=dry_run, live=live)
    allowed = cap < 0 or (cap > 0 and open_count < cap)
    return PositionLimitDecision(
        allowed=allowed,
        lane=normalized,
        open_count=open_count,
        cap=cap,
        reason="ok" if allowed else f"lane_cap:{normalized}",
    )


def describe_position_limits() -> dict[str, Any]:
    return {
        "green_sniper": {
            "paper": _cap_for_lane(LANE_PUMP_EARLY_GREEN_SNIPER, dry_run=True, live=False),
            "live": _cap_for_lane(LANE_PUMP_EARLY_GREEN_SNIPER, dry_run=False, live=True),
        },
        "pumpswap_profit": {
            "paper": _cap_for_lane(LANE_PUMP_EARLY_PROFIT, dry_run=True, live=False),
            "live": _cap_for_lane(LANE_PUMP_EARLY_PROFIT, dry_run=False, live=True),
        },
        "breakout": {
            "paper": _cap_for_lane(LANE_PUMP_EARLY_BREAKOUT, dry_run=True, live=False),
            "live": _cap_for_lane(LANE_PUMP_EARLY_BREAKOUT, dry_run=False, live=True),
        },
        "pumpswap_rebound_prime": {
            "paper": _cap_for_lane(LANE_PUMPSWAP_REBOUND_PRIME, dry_run=True, live=False),
            "live": _cap_for_lane(LANE_PUMPSWAP_REBOUND_PRIME, dry_run=False, live=True),
        },
        "research_rank_canary": {
            "paper": _cap_for_lane(LANE_RESEARCH_RANK_CANARY, dry_run=True, live=False),
            "live": _cap_for_lane(LANE_RESEARCH_RANK_CANARY, dry_run=False, live=True),
        },
        "late_momentum_watch": {
            "paper": _cap_for_lane(LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH, dry_run=True, live=False),
            "live": _cap_for_lane(LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH, dry_run=False, live=True),
        },
        "birth_probe_micro_canary": {
            "paper": _cap_for_lane(LANE_BIRTH_PROBE_MICRO_CANARY, dry_run=True, live=False),
            "live": _cap_for_lane(LANE_BIRTH_PROBE_MICRO_CANARY, dry_run=False, live=True),
        },
        "moonshot_micro_lottery": {
            "paper": _cap_for_lane(LANE_MOONSHOT_MICRO_LOTTERY, dry_run=True, live=False),
            "live": _cap_for_lane(LANE_MOONSHOT_MICRO_LOTTERY, dry_run=False, live=True),
        },
        "shadow_followup_micro": {
            "paper": _cap_for_lane(LANE_SHADOW_FOLLOWUP_MICRO, dry_run=True, live=False),
            "live": _cap_for_lane(LANE_SHADOW_FOLLOWUP_MICRO, dry_run=False, live=True),
        },
    }


__all__ = ["PositionLimitDecision", "count_open_by_lane", "describe_position_limits", "evaluate_lane_position_limit"]
