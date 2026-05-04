from __future__ import annotations

from typing import Any, Mapping

from ml.lane_taxonomy import (
    LANE_PUMP_EARLY_BIRTH_PROBE,
    LANE_PUMP_EARLY_GREEN_SNIPER,
    LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH,
    LANE_RESEARCH_RANK_CANARY,
    LANE_RESEARCH_SNIPER,
    normalize_entry_lane,
)


POLICY_GREEN_SNIPER_PURE = "green_sniper_pure"
POLICY_GREEN_SNIPER_RESTRICTED_BUY = "green_sniper_restricted_buy"
POLICY_GREEN_SNIPER_SHADOW = "green_sniper_shadow"
POLICY_RESEARCH_RANK_CANARY = "research_rank_canary"
POLICY_PUMP_EARLY_SNIPER_RESEARCH = "pump_early_sniper_research"
POLICY_PAPER_BIRTH_PROBE = "paper_birth_probe"
POLICY_LATE_MOMENTUM_WATCH = "late_momentum_watch"
POLICY_UNKNOWN = "unknown"

POLICY_CATEGORIES = (
    POLICY_GREEN_SNIPER_PURE,
    POLICY_GREEN_SNIPER_RESTRICTED_BUY,
    POLICY_GREEN_SNIPER_SHADOW,
    POLICY_RESEARCH_RANK_CANARY,
    POLICY_PUMP_EARLY_SNIPER_RESEARCH,
    POLICY_PAPER_BIRTH_PROBE,
    POLICY_LATE_MOMENTUM_WATCH,
)


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def _action(row: Mapping[str, Any]) -> str:
    return _text(row.get("green_sniper_action") or row.get("decision_action") or row.get("action") or row.get("decision"))


def classify_policy_category(row: Mapping[str, Any]) -> str:
    explicit = _text(row.get("lane_policy_category") or row.get("policy_category"))
    if explicit in POLICY_CATEGORIES:
        return explicit

    gate = _text(row.get("gate_profile") or row.get("sniper_gate_profile") or row.get("live_profit_gate_profile"))
    subtype = _text(row.get("entry_subtype"))
    reason = _text(row.get("green_sniper_reason") or row.get("reason") or row.get("reject_reason"))
    sample_type = _text(row.get("sample_type"))
    lane = normalize_entry_lane(row.get("entry_lane") or row.get("lane") or row.get("profit_lane_tier"))
    tier = normalize_entry_lane(row.get("profit_lane_tier") or row.get("size_bucket"))
    action = _action(row)

    if subtype == "paper_birth_probe" or "birth_probe" in gate or lane == LANE_PUMP_EARLY_BIRTH_PROBE:
        return POLICY_PAPER_BIRTH_PROBE
    if lane == LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH or "late_momentum" in gate or "late_momentum" in sample_type:
        return POLICY_LATE_MOMENTUM_WATCH
    if lane == LANE_RESEARCH_RANK_CANARY or tier == LANE_RESEARCH_RANK_CANARY or "research_rank_canary" in gate:
        return POLICY_RESEARCH_RANK_CANARY
    if lane == LANE_RESEARCH_SNIPER or tier == LANE_RESEARCH_SNIPER:
        return POLICY_PUMP_EARLY_SNIPER_RESEARCH
    if lane == LANE_PUMP_EARLY_GREEN_SNIPER or gate.startswith("green_sniper"):
        if gate == POLICY_GREEN_SNIPER_RESTRICTED_BUY or "restricted_buy" in reason:
            return POLICY_GREEN_SNIPER_RESTRICTED_BUY
        if "shadow" in action or "shadow" in sample_type or "shadow" in reason:
            return POLICY_GREEN_SNIPER_SHADOW
        return POLICY_GREEN_SNIPER_PURE
    return POLICY_UNKNOWN


__all__ = [
    "POLICY_CATEGORIES",
    "POLICY_GREEN_SNIPER_PURE",
    "POLICY_GREEN_SNIPER_RESTRICTED_BUY",
    "POLICY_GREEN_SNIPER_SHADOW",
    "POLICY_LATE_MOMENTUM_WATCH",
    "POLICY_PAPER_BIRTH_PROBE",
    "POLICY_PUMP_EARLY_SNIPER_RESEARCH",
    "POLICY_RESEARCH_RANK_CANARY",
    "POLICY_UNKNOWN",
    "classify_policy_category",
]
