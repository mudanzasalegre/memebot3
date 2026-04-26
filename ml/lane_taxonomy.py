from __future__ import annotations

from typing import Any


LANE_PUMP_EARLY_PROFIT = "pump_early_pumpswap_profit"
LANE_PUMP_EARLY_PRIME = "pump_early_pumpswap_prime"
LANE_PUMP_EARLY_METEOR = "pump_early_meteor_prime"
LANE_PUMP_EARLY_BREAKOUT = "pump_early_pumpswap_breakout_probe"
LANE_RESEARCH_SNIPER = "pump_early_sniper_research"
LANE_UNKNOWN = "unknown"

LIVE_PROFIT_LANES = {
    LANE_PUMP_EARLY_PROFIT,
    LANE_PUMP_EARLY_PRIME,
    LANE_PUMP_EARLY_METEOR,
    LANE_PUMP_EARLY_BREAKOUT,
}

RESEARCH_LANES = {
    LANE_RESEARCH_SNIPER,
}

TRAINABLE_LANES = LIVE_PROFIT_LANES | RESEARCH_LANES

_LANE_ALIASES = {
    LANE_PUMP_EARLY_PROFIT: LANE_PUMP_EARLY_PROFIT,
    "pumpswap_profit": LANE_PUMP_EARLY_PROFIT,
    "pumpswap_profit_broad": LANE_PUMP_EARLY_PROFIT,
    LANE_PUMP_EARLY_PRIME: LANE_PUMP_EARLY_PRIME,
    "pumpswap_prime": LANE_PUMP_EARLY_PRIME,
    "pumpswap_profit_prime": LANE_PUMP_EARLY_PRIME,
    LANE_PUMP_EARLY_METEOR: LANE_PUMP_EARLY_METEOR,
    "pumpswap_meteor": LANE_PUMP_EARLY_METEOR,
    "pumpswap_meteor_prime": LANE_PUMP_EARLY_METEOR,
    LANE_PUMP_EARLY_BREAKOUT: LANE_PUMP_EARLY_BREAKOUT,
    "pumpswap_breakout": LANE_PUMP_EARLY_BREAKOUT,
    "pumpswap_breakout_probe": LANE_PUMP_EARLY_BREAKOUT,
    LANE_RESEARCH_SNIPER: LANE_RESEARCH_SNIPER,
    "pump_early_sniper": LANE_RESEARCH_SNIPER,
    "sniper": LANE_RESEARCH_SNIPER,
    "sniper_micro": LANE_RESEARCH_SNIPER,
    "sniper_core": LANE_RESEARCH_SNIPER,
    "sniper_hot": LANE_RESEARCH_SNIPER,
}


def _norm_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def normalize_entry_lane(value: Any) -> str:
    raw = _norm_key(value)
    if not raw or raw in {"none", "nan", "<na>"}:
        return LANE_UNKNOWN
    return _LANE_ALIASES.get(raw, raw if raw in TRAINABLE_LANES else raw)


def lane_group(lane: Any) -> str:
    normalized = normalize_entry_lane(lane)
    if normalized in LIVE_PROFIT_LANES:
        return "live_profit"
    if normalized in RESEARCH_LANES:
        return "research"
    return "unknown"


def is_live_profit_lane(lane: Any) -> bool:
    return normalize_entry_lane(lane) in LIVE_PROFIT_LANES


def is_research_lane(lane: Any) -> bool:
    return normalize_entry_lane(lane) in RESEARCH_LANES


__all__ = [
    "LANE_PUMP_EARLY_PROFIT",
    "LANE_PUMP_EARLY_PRIME",
    "LANE_PUMP_EARLY_METEOR",
    "LANE_PUMP_EARLY_BREAKOUT",
    "LANE_RESEARCH_SNIPER",
    "LANE_UNKNOWN",
    "LIVE_PROFIT_LANES",
    "RESEARCH_LANES",
    "TRAINABLE_LANES",
    "normalize_entry_lane",
    "lane_group",
    "is_live_profit_lane",
    "is_research_lane",
]
