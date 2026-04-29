from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from config.config import CFG
from ml.lane_taxonomy import (
    LANE_PUMP_EARLY_BREAKOUT,
    LANE_PUMP_EARLY_GREEN_SNIPER,
    LANE_PUMP_EARLY_PROFIT,
    LANE_RESEARCH_SNIPER,
    normalize_entry_lane,
)


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
            return int(getattr(CFG, "GREEN_SNIPER_LIVE_MAX_OPEN", 1) or 1)
        return int(getattr(CFG, "GREEN_SNIPER_MAX_OPEN_PAPER", getattr(CFG, "PUMP_EARLY_SNIPER_MAX_OPEN_PAPER", 6)) or 6)
    if lane == LANE_PUMP_EARLY_BREAKOUT:
        return int(
            getattr(
                CFG,
                "PUMP_EARLY_BREAKOUT_MAX_OPEN_LIVE_CANARY" if live else "PUMP_EARLY_BREAKOUT_MAX_OPEN_PAPER",
                1,
            )
            or 1
        )
    if lane == LANE_PUMP_EARLY_PROFIT:
        return int(
            getattr(
                CFG,
                "PUMP_EARLY_PROFIT_MAX_OPEN_LIVE_CANARY" if live else "PUMP_EARLY_PROFIT_MAX_OPEN_PAPER",
                2,
            )
            or 2
        )
    if lane == LANE_RESEARCH_SNIPER:
        return int(getattr(CFG, "RESEARCH_SHADOW_MAX_OPEN_PER_REGIME", 4) or 4)
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
    cap = max(0, _cap_for_lane(normalized, dry_run=dry_run, live=live))
    allowed = cap <= 0 or open_count < cap
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
    }


__all__ = ["PositionLimitDecision", "count_open_by_lane", "describe_position_limits", "evaluate_lane_position_limit"]
