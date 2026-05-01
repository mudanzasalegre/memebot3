from __future__ import annotations

from typing import Any

from config.config import CFG
from ml.lane_taxonomy import (
    LANE_PUMP_EARLY_GREEN_SNIPER,
    LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH,
    LANE_RESEARCH_RANK_CANARY,
    normalize_entry_lane,
)

POLICY_MODES = {"observe", "shadow", "canary", "sizing_only", "enforce"}


def _mode(value: Any, default: str) -> str:
    raw = str(value or default).strip().lower()
    return raw if raw in POLICY_MODES else default


def mode_for_lane(lane: str, *, live: bool = False) -> str:
    lane = normalize_entry_lane(lane)
    if lane == LANE_PUMP_EARLY_GREEN_SNIPER:
        default = "sizing_only"
        mode = _mode(getattr(CFG, "GREEN_SNIPER_POLICY_MODE", default), default)
    elif lane == LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH:
        default = "shadow"
        mode = _mode(getattr(CFG, "LATE_MOMENTUM_POLICY_MODE", default), default)
    elif lane == LANE_RESEARCH_RANK_CANARY:
        default = "canary"
        mode = _mode(getattr(CFG, "RESEARCH_RANK_POLICY_MODE", default), default)
    else:
        mode = _mode(getattr(CFG, "DEFAULT_POLICY_MODE", "observe"), "observe")
    if live and mode == "enforce" and not bool(getattr(CFG, "ALLOW_LIVE_POLICY_ENFORCE", False)):
        return "shadow"
    return mode


def action_for_mode(*, base_action: str, policy_action: str, mode: str) -> str:
    mode = _mode(mode, "observe")
    if mode == "observe":
        return base_action
    if mode == "shadow":
        return "shadow" if policy_action == "buy" else policy_action
    if mode == "sizing_only":
        return base_action
    if mode == "canary":
        return policy_action if policy_action in {"buy", "shadow", "reject", "delay"} else base_action
    if mode == "enforce":
        return policy_action
    return base_action


__all__ = ["POLICY_MODES", "action_for_mode", "mode_for_lane"]
