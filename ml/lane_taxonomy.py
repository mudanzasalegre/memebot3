from __future__ import annotations

from typing import Any


LANE_PUMP_EARLY_PROFIT = "pump_early_pumpswap_profit"
LANE_PUMP_EARLY_PRIME = "pump_early_pumpswap_prime"
LANE_PUMP_EARLY_METEOR = "pump_early_meteor_prime"
LANE_PUMP_EARLY_BREAKOUT = "pump_early_pumpswap_breakout_probe"
LANE_PUMPSWAP_REBOUND_PRIME = "pump_early_pumpswap_rebound_prime"
LANE_PUMP_EARLY_GREEN_SNIPER = "pump_early_green_candle_sniper"
LANE_PUMP_EARLY_BIRTH_PROBE = "pump_early_birth_probe"
LANE_BIRTH_PROBE_MICRO_CANARY = "pump_early_birth_probe_micro_canary"
LANE_MOONSHOT_MICRO_LOTTERY = "pump_early_moonshot_micro_lottery"
LANE_SHADOW_FOLLOWUP_MICRO = "pump_early_shadow_followup_micro"
LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH = "pump_early_late_momentum_watch"
LANE_RESEARCH_SNIPER = "pump_early_sniper_research"
LANE_RESEARCH_RANK_CANARY = "pump_early_research_rank_canary"
LANE_UNKNOWN = "unknown"

LIVE_PROFIT_LANES = {
    LANE_PUMP_EARLY_PROFIT,
    LANE_PUMP_EARLY_PRIME,
    LANE_PUMP_EARLY_METEOR,
    LANE_PUMP_EARLY_BREAKOUT,
    LANE_PUMPSWAP_REBOUND_PRIME,
    LANE_PUMP_EARLY_GREEN_SNIPER,
    LANE_RESEARCH_RANK_CANARY,
}

RESEARCH_LANES = {
    LANE_RESEARCH_SNIPER,
    LANE_PUMP_EARLY_BIRTH_PROBE,
    LANE_BIRTH_PROBE_MICRO_CANARY,
    LANE_MOONSHOT_MICRO_LOTTERY,
    LANE_SHADOW_FOLLOWUP_MICRO,
    LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH,
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
    LANE_PUMPSWAP_REBOUND_PRIME: LANE_PUMPSWAP_REBOUND_PRIME,
    "pumpswap_rebound": LANE_PUMPSWAP_REBOUND_PRIME,
    "pumpswap_rebound_prime": LANE_PUMPSWAP_REBOUND_PRIME,
    LANE_PUMP_EARLY_GREEN_SNIPER: LANE_PUMP_EARLY_GREEN_SNIPER,
    "green_sniper": LANE_PUMP_EARLY_GREEN_SNIPER,
    "green_candle": LANE_PUMP_EARLY_GREEN_SNIPER,
    "pumpswap_green": LANE_PUMP_EARLY_GREEN_SNIPER,
    "hot_green": LANE_PUMP_EARLY_GREEN_SNIPER,
    "newborn_pump": LANE_PUMP_EARLY_GREEN_SNIPER,
    LANE_PUMP_EARLY_BIRTH_PROBE: LANE_PUMP_EARLY_BIRTH_PROBE,
    "paper_birth_probe": LANE_PUMP_EARLY_BIRTH_PROBE,
    "birth_probe": LANE_PUMP_EARLY_BIRTH_PROBE,
    LANE_BIRTH_PROBE_MICRO_CANARY: LANE_BIRTH_PROBE_MICRO_CANARY,
    "birth_probe_micro_canary": LANE_BIRTH_PROBE_MICRO_CANARY,
    "paper_birth_probe_micro_canary": LANE_BIRTH_PROBE_MICRO_CANARY,
    LANE_MOONSHOT_MICRO_LOTTERY: LANE_MOONSHOT_MICRO_LOTTERY,
    "moonshot_micro_lottery": LANE_MOONSHOT_MICRO_LOTTERY,
    "pump_early_moonshot": LANE_MOONSHOT_MICRO_LOTTERY,
    LANE_SHADOW_FOLLOWUP_MICRO: LANE_SHADOW_FOLLOWUP_MICRO,
    "shadow_followup_micro": LANE_SHADOW_FOLLOWUP_MICRO,
    "pump_early_shadow_followup": LANE_SHADOW_FOLLOWUP_MICRO,
    "shadow_followup_momentum": LANE_SHADOW_FOLLOWUP_MICRO,
    LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH: LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH,
    "late_momentum_watch": LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH,
    LANE_RESEARCH_SNIPER: LANE_RESEARCH_SNIPER,
    "pump_early_sniper": LANE_RESEARCH_SNIPER,
    "sniper": LANE_RESEARCH_SNIPER,
    "sniper_micro": LANE_RESEARCH_SNIPER,
    "sniper_core": LANE_RESEARCH_SNIPER,
    "sniper_hot": LANE_RESEARCH_SNIPER,
    LANE_RESEARCH_RANK_CANARY: LANE_RESEARCH_RANK_CANARY,
    "research_rank_canary": LANE_RESEARCH_RANK_CANARY,
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
    "LANE_PUMPSWAP_REBOUND_PRIME",
    "LANE_PUMP_EARLY_GREEN_SNIPER",
    "LANE_PUMP_EARLY_BIRTH_PROBE",
    "LANE_BIRTH_PROBE_MICRO_CANARY",
    "LANE_MOONSHOT_MICRO_LOTTERY",
    "LANE_SHADOW_FOLLOWUP_MICRO",
    "LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH",
    "LANE_RESEARCH_SNIPER",
    "LANE_RESEARCH_RANK_CANARY",
    "LANE_UNKNOWN",
    "LIVE_PROFIT_LANES",
    "RESEARCH_LANES",
    "TRAINABLE_LANES",
    "normalize_entry_lane",
    "lane_group",
    "is_live_profit_lane",
    "is_research_lane",
]
