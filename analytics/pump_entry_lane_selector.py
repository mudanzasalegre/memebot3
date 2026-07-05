from __future__ import annotations

import collections
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analytics.lane_sizing import LANE_SHADOW_FOLLOWUP_MICRO
from analytics.report_utils import (
    boolish,
    fnum,
    load_candidate_outcomes,
    load_runtime_events,
    metrics_dir,
    write_json,
)
from analytics.shadow_followup_micro import evaluate_shadow_followup_micro
from config.config import CFG, PROJECT_ROOT
from ml.lane_taxonomy import (
    LANE_MOONSHOT_MICRO_LOTTERY,
    LANE_PAPER_BOOTSTRAP_MICRO,
    LANE_PAPER_EXPLORATION_MICRO,
    LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH,
    LANE_RESEARCH_RANK_CANARY,
    LANE_RESEARCH_SNIPER,
    LANE_SNIPER_RESEARCH_MICRO_FALLBACK,
    normalize_entry_lane,
)


@dataclass(frozen=True)
class PumpEntryLaneDecision:
    allowed: bool
    selected_lane: str
    reason: str
    shadow_kind: str = "shadow"
    amount_cap_sol: float | None = None


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _text(row: dict[str, Any], *keys: str) -> str:
    return " ".join(str(_first(row, key) or "") for key in keys).strip().lower()


def _cfg_float(cfg: Any, key: str, default: float) -> float:
    return fnum(getattr(cfg, key, default), default)


def _lane(row: dict[str, Any]) -> str:
    return normalize_entry_lane(_first(row, "entry_lane", "lane", "profit_lane_tier"))


def _is_pump_candidate(row: dict[str, Any]) -> bool:
    haystack = _text(row, "entry_regime", "source", "discovered_via", "dex_id", "dexId", "address", "entry_lane")
    return any(term in haystack for term in ("pump_early", "pumpfun", "pump_fun", "pumpswap", "pump"))


def _rank_priority(row: dict[str, Any]) -> bool:
    rank = fnum(_first(row, "research_rank_canary_rank_score", "rank_score", "research_rank_score"), 0.0)
    if 0.0 < rank <= 1.0:
        rank *= 100.0
    return (
        rank >= fnum(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE", 70.0), 70.0)
        and fnum(_first(row, "txns_last_5m", "buy_txns_last_5m", "txns_5m"), 0.0)
        >= fnum(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MIN_TXNS_5M", 1000), 1000.0)
        and fnum(_first(row, "liquidity_usd", "buy_liquidity_usd"), 0.0)
        >= fnum(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MIN_LIQUIDITY_USD", 20_000.0), 20_000.0)
        and 20_000.0 <= fnum(_first(row, "market_cap_usd", "buy_market_cap_usd", "mcap"), 0.0) <= 120_000.0
        and boolish(_first(row, "has_jupiter_route", "route_ok", "route_available"), False)
        and not boolish(_first(row, "liquidity_is_proxy", "liquidity_usd_is_proxy", "buy_liquidity_is_proxy"), False)
    )


def _confirmed(row: dict[str, Any]) -> bool:
    text = _text(row, "reason", "green_sniper_reason", "entry_reason", "sniper_research_subprofile_reason")
    if "confirmed" in text or "paper_exploration_quota" in text:
        return True
    return boolish(_first(row, "confirmed", "entry_confirmed", "shadow_followup_confirmed"), False)


def select_pump_entry_lane(row: dict[str, Any], *, cfg: Any = CFG) -> PumpEntryLaneDecision:
    if not _is_pump_candidate(row):
        return PumpEntryLaneDecision(True, _lane(row), "non_pump_candidate")

    lane = _lane(row)
    gate = str(_first(row, "gate_profile", "sniper_gate_profile", "live_profit_gate_profile") or "").strip().lower()
    reason = _text(row, "reason", "green_sniper_reason", "entry_reason", "sniper_research_subprofile_reason")
    mcap = fnum(_first(row, "market_cap_usd", "buy_market_cap_usd", "mcap"), 0.0)
    cluster_bad = boolish(_first(row, "cluster_bad", "helius_cluster_bad"), False) or "cluster_bad" in reason
    amount = fnum(
        _first(
            row,
            "amount_sol",
            "buy_amount_sol",
            "sniper_research_micro_fallback_amount_sol",
            "shadow_followup_micro_amount_sol",
            "moonshot_micro_lottery_amount_sol",
        ),
        0.0,
    )
    rank_cap = _cfg_float(cfg, "RESEARCH_RANK_CANARY_MAX_SIZE_SOL", 0.03)
    bootstrap_cap = min(
        _cfg_float(cfg, "PAPER_BOOTSTRAP_AMOUNT_SOL", 0.1),
        _cfg_float(cfg, "PAPER_BOOTSTRAP_MAX_AMOUNT_SOL", 0.1),
    )
    paper_exploration_cap = _cfg_float(
        cfg,
        "PAPER_IDLE_AMOUNT_SOL",
        _cfg_float(cfg, "PAPER_EXPLORATION_AMOUNT_SOL", 0.1),
    )
    shadow_followup_cap = _cfg_float(cfg, "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL", 0.003)
    sniper_fallback_cap = _cfg_float(cfg, "SNIPER_RESEARCH_MICRO_FALLBACK_AMOUNT_SOL", 0.003)
    late_momentum_cap = _cfg_float(cfg, "LATE_MOMENTUM_MICRO_AMOUNT_SOL", 0.003)
    moonshot_cap = _cfg_float(cfg, "MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL", 0.001)
    cluster_safe_moonshot_cap = max(
        _cfg_float(cfg, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_AMOUNT_SOL", 0.001),
        _cfg_float(cfg, "MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_AMOUNT_SOL", 0.0005),
    )

    shadow_followup_decision = evaluate_shadow_followup_micro(row, cfg=cfg)
    if shadow_followup_decision.allowed:
        return PumpEntryLaneDecision(True, LANE_SHADOW_FOLLOWUP_MICRO, "shadow_followup_momentum", amount_cap_sol=shadow_followup_cap)

    if lane == "unknown" or not gate:
        return PumpEntryLaneDecision(False, "shadow", "untagged_buy_blocked", "execution")

    if _rank_priority(row):
        return PumpEntryLaneDecision(True, LANE_RESEARCH_RANK_CANARY, "research_rank_canary_priority", amount_cap_sol=rank_cap)

    if lane == LANE_PAPER_BOOTSTRAP_MICRO or "paper_bootstrap" in reason or gate == "paper_bootstrap":
        return PumpEntryLaneDecision(True, LANE_PAPER_BOOTSTRAP_MICRO, "paper_bootstrap", amount_cap_sol=bootstrap_cap)

    if cluster_bad:
        allowed_cluster_moonshot = (
            lane == LANE_MOONSHOT_MICRO_LOTTERY
            and "confirmed" in reason
            and amount > 0.0
            and amount <= cluster_safe_moonshot_cap
        )
        if not allowed_cluster_moonshot:
            return PumpEntryLaneDecision(False, "shadow", "cluster_bad_shadow_only", "cluster_bad")

    if lane == LANE_SHADOW_FOLLOWUP_MICRO or "shadow_followup" in reason:
        return PumpEntryLaneDecision(True, LANE_SHADOW_FOLLOWUP_MICRO, "shadow_followup_momentum", amount_cap_sol=shadow_followup_cap)

    if lane == LANE_PAPER_EXPLORATION_MICRO or "paper_exploration_quota" in reason or gate == "paper_exploration_quota":
        return PumpEntryLaneDecision(True, LANE_PAPER_EXPLORATION_MICRO, "paper_exploration_quota", amount_cap_sol=paper_exploration_cap)

    if (
        lane == LANE_SNIPER_RESEARCH_MICRO_FALLBACK
        or gate == "sniper_research_micro_fallback"
        or "sniper_research_micro_fallback" in reason
    ):
        return PumpEntryLaneDecision(
            True,
            LANE_SNIPER_RESEARCH_MICRO_FALLBACK,
            "sniper_research_micro_fallback",
            amount_cap_sol=sniper_fallback_cap,
        )

    if lane == LANE_RESEARCH_SNIPER:
        sub = str(_first(row, "entry_subprofile", "sniper_research_subprofile") or "").strip().lower()
        if "sniper_research_micro_fallback" in sub:
            return PumpEntryLaneDecision(
                True,
                LANE_SNIPER_RESEARCH_MICRO_FALLBACK,
                "sniper_research_micro_fallback",
                amount_cap_sol=sniper_fallback_cap,
            )
        if "momentum_ignition" in sub:
            if mcap > 100_000.0:
                return PumpEntryLaneDecision(False, "shadow", "momentum_mcap_gt_100k", "research")
            if _confirmed(row):
                return PumpEntryLaneDecision(
                    True,
                    LANE_RESEARCH_SNIPER,
                    "sniper_research_momentum_ignition_confirmed",
                    amount_cap_sol=_cfg_float(cfg, "SNIPER_RESEARCH_MOMENTUM_SIZE_SOL", _cfg_float(cfg, "SNIPER_RESEARCH_SIZE_SOL", 0.005)),
                )
        if "deep_reversal" in sub and _confirmed(row):
            return PumpEntryLaneDecision(
                True,
                LANE_RESEARCH_SNIPER,
                "sniper_research_deep_reversal_confirmed",
                amount_cap_sol=_cfg_float(cfg, "SNIPER_RESEARCH_DEEP_REVERSAL_SIZE_SOL", _cfg_float(cfg, "SNIPER_RESEARCH_SIZE_SOL", 0.005)),
            )

    if lane == LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH and _confirmed(row):
        return PumpEntryLaneDecision(True, lane, "late_momentum_micro_confirmed", amount_cap_sol=late_momentum_cap)

    if lane == LANE_MOONSHOT_MICRO_LOTTERY and ("confirmed" in reason or boolish(row.get("moonshot_micro_lottery"), False)):
        return PumpEntryLaneDecision(True, lane, "moonshot_micro_lottery_confirmed", amount_cap_sol=moonshot_cap)

    if gate in {"pumpswap_prime_strict", "pumpswap_profit_prime"} or "pumpswap_prime_strict" in reason:
        return PumpEntryLaneDecision(False, "shadow", "pumpswap_strict_no_sublane", "pumpswap_strict")

    return PumpEntryLaneDecision(False, "shadow", "pump_entry_no_approved_sublane", "shadow")


def build_pump_entry_lane_selector_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_runtime_events(root) + load_candidate_outcomes(root)
    candidates = [row for row in rows if _is_pump_candidate(row)]
    decisions = [select_pump_entry_lane(row) for row in candidates]
    selected = collections.Counter(decision.selected_lane for decision in decisions if decision.allowed)
    shadow = collections.Counter(decision.reason for decision in decisions if not decision.allowed)
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "candidates_seen": len(candidates),
        "selected_by_lane": dict(selected.most_common()),
        "shadow_by_reason": dict(shadow.most_common()),
        "strict_no_sublane": int(shadow.get("pumpswap_strict_no_sublane") or 0),
        "rank_priority": int(selected.get(LANE_RESEARCH_RANK_CANARY) or 0),
        "shadow_followup": int(selected.get(LANE_SHADOW_FOLLOWUP_MICRO) or 0),
        "paper_exploration": int(selected.get(LANE_PAPER_EXPLORATION_MICRO) or 0),
        "paper_bootstrap": int(selected.get(LANE_PAPER_BOOTSTRAP_MICRO) or 0),
        "sniper_research_micro_fallback": int(selected.get(LANE_SNIPER_RESEARCH_MICRO_FALLBACK) or 0),
        "momentum_confirmed": sum(1 for decision in decisions if decision.reason == "sniper_research_momentum_ignition_confirmed"),
        "deep_reversal_confirmed": sum(1 for decision in decisions if decision.reason == "sniper_research_deep_reversal_confirmed"),
        "moonshot_confirmed": sum(1 for decision in decisions if decision.reason == "moonshot_micro_lottery_confirmed"),
    }


def write_pump_entry_lane_selector_report(root: Path | None = None) -> dict[str, Any]:
    report = build_pump_entry_lane_selector_report(root)
    write_json(metrics_dir(root) / "pump_entry_lane_selector_report.json", report)
    return report


__all__ = [
    "PumpEntryLaneDecision",
    "build_pump_entry_lane_selector_report",
    "select_pump_entry_lane",
    "write_pump_entry_lane_selector_report",
]
