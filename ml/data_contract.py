from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import pandas as pd

from ml.lane_taxonomy import (
    LANE_PUMP_EARLY_BREAKOUT,
    LANE_PUMP_EARLY_BIRTH_PROBE,
    LANE_PUMP_EARLY_GREEN_SNIPER,
    LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH,
    LANE_PUMP_EARLY_METEOR,
    LANE_PUMP_EARLY_PRIME,
    LANE_PUMP_EARLY_PROFIT,
    LANE_RESEARCH_SNIPER,
    LANE_SNIPER_RESEARCH_MICRO_FALLBACK,
    LANE_UNKNOWN,
    normalize_entry_lane,
)


SAMPLE_TRADE_CLOSE = "trade_close"
SAMPLE_SHADOW_CLOSE = "shadow_close"
SAMPLE_POLICY_REJECT = "policy_reject"
SAMPLE_CANDIDATE = "candidate"
SAMPLE_EXECUTION_BLOCKED_NO_ROUTE = "execution_blocked_no_route"
SAMPLE_EXECUTION_BLOCKED_ZERO_QTY = "execution_blocked_zero_qty"
SAMPLE_GREEN_SNIPER_REJECT_SHADOW = "green_sniper_reject_shadow"
SAMPLE_LATE_MOMENTUM_WATCH_SHADOW = "late_momentum_watch_shadow"
SAMPLE_RESEARCH_RANK_SHADOW = "research_rank_shadow"
SAMPLE_UNKNOWN = "unknown"

VALID_SAMPLE_TYPES = {
    SAMPLE_TRADE_CLOSE,
    SAMPLE_SHADOW_CLOSE,
    SAMPLE_POLICY_REJECT,
    SAMPLE_CANDIDATE,
    SAMPLE_EXECUTION_BLOCKED_NO_ROUTE,
    SAMPLE_EXECUTION_BLOCKED_ZERO_QTY,
    SAMPLE_GREEN_SNIPER_REJECT_SHADOW,
    SAMPLE_LATE_MOMENTUM_WATCH_SHADOW,
    SAMPLE_RESEARCH_RANK_SHADOW,
}

REQUIRED_ML_CONTEXT_COLUMNS = (
    "address",
    "mint",
    "timestamp",
    "sample_type",
    "decision_id",
    "candidate_stage",
    "decision",
    "outcome",
    "blockers",
    "feature_snapshot",
    "run_id",
    "source",
    "lane",
    "entry_regime",
    "entry_lane",
    "gate_profile",
    "profit_lane_tier",
    "dex_id",
    "price_source",
    "label",
    "target_total_pnl_pct",
)

_SNAPSHOT_EXCLUDE_KEYS = {
    "action",
    "blocker",
    "blockers",
    "candidate_stage",
    "decision",
    "decision_action",
    "decision_id",
    "decision_intent",
    "event_type",
    "feature_snapshot",
    "features_snapshot",
    "linked_decision_id",
    "outcome",
    "reason",
    "raw_action",
    "row_lineage",
    "stage",
    "timestamp",
    "ts_utc",
}

_BUY_EXECUTION_EVIDENCE_FIELDS = (
    "order_id",
    "execution_id",
    "trade_id",
    "position_id",
    "buy_tx_sig",
    "tx_sig",
    "tx_signature",
)

_BLOCKER_ALIASES = {
    "": "",
    "none": "",
    "unknown": "",
    "ok": "",
    "no_liq": "liquidity_missing",
    "liq_missing": "liquidity_missing",
    "liquidity_missing": "liquidity_missing",
    "no_route": "route_missing",
    "jupiter_price_missing": "price_missing",
    "no_jup_price": "price_missing",
    "price_missing": "price_missing",
    "provider_degraded": "provider_degraded",
    "buy_zero_qty": "execution_zero_qty",
    "zero_qty": "execution_zero_qty",
    "basic_filter": "basic_filter",
    "banned_creator": "banned_creator",
    "dex_whitelist": "dex_whitelist",
    "cluster_bad": "cluster_bad",
    "ml_gate": "ml_gate",
    "soft_score": "soft_score",
}


def _raw(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _has_buy_execution_evidence(row: Mapping[str, Any]) -> bool:
    event_type = _raw(row.get("event_type")).lower().replace("-", "_").replace(" ", "_")
    if event_type in {"buy", "bought", "buy_ok", "paper_buy"}:
        return True
    if any(_raw(row.get(key)) for key in _BUY_EXECUTION_EVIDENCE_FIELDS):
        return True
    if event_type != "execution" or not _raw(row.get("side")).lower().startswith("buy"):
        return False
    ok = row.get("ok")
    if isinstance(ok, str):
        return ok.strip().lower() in {"1", "true", "yes", "ok", "success"}
    return bool(ok)


def _decision_value(row: Mapping[str, Any]) -> Any:
    for key in ("decision_action", "action", "decision"):
        value = row.get(key)
        if _raw(value):
            return value
    return None


def normalize_sample_type(value: Any) -> str:
    raw = _raw(value).lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return SAMPLE_UNKNOWN
    aliases = {
        "trade": SAMPLE_TRADE_CLOSE,
        "live_trade": SAMPLE_TRADE_CLOSE,
        "closed_trade": SAMPLE_TRADE_CLOSE,
        "shadow": SAMPLE_SHADOW_CLOSE,
        "research_shadow": SAMPLE_SHADOW_CLOSE,
        "reject": SAMPLE_POLICY_REJECT,
        "policy_rejected": SAMPLE_POLICY_REJECT,
        "candidate_reject": SAMPLE_POLICY_REJECT,
        "no_route": SAMPLE_EXECUTION_BLOCKED_NO_ROUTE,
        "execution_blocked_no_route": SAMPLE_EXECUTION_BLOCKED_NO_ROUTE,
        "zero_qty": SAMPLE_EXECUTION_BLOCKED_ZERO_QTY,
        "execution_blocked_zero_qty": SAMPLE_EXECUTION_BLOCKED_ZERO_QTY,
        "green_sniper_reject_shadow": SAMPLE_GREEN_SNIPER_REJECT_SHADOW,
        "late_momentum_watch_shadow": SAMPLE_LATE_MOMENTUM_WATCH_SHADOW,
        "late_momentum_shadow": SAMPLE_LATE_MOMENTUM_WATCH_SHADOW,
        "research_rank_shadow": SAMPLE_RESEARCH_RANK_SHADOW,
        "research_rank_canary_shadow": SAMPLE_RESEARCH_RANK_SHADOW,
    }
    return aliases.get(raw, raw if raw in VALID_SAMPLE_TYPES else SAMPLE_UNKNOWN)


def normalize_entry_regime(value: Any) -> str:
    raw = _raw(value).lower().replace("-", "_").replace(" ", "_")
    if raw in {"pump_early", "pump", "pumpfun", "pump_fun"}:
        return "pump_early"
    if raw in {"revival", "revive", "revived"}:
        return "revival"
    return "dex_mature"


def normalize_dex_id(value: Any) -> str:
    raw = _raw(value).lower().replace("_", "").replace("-", "").replace(" ", "")
    aliases = {
        "pump": "pumpfun",
        "pumpfun": "pumpfun",
        "pumpswap": "pumpswap",
        "pumpamm": "pumpswap",
        "meteora": "meteora",
        "meteor": "meteora",
        "raydium": "raydium",
        "orca": "orca",
    }
    return aliases.get(raw, raw or "unknown")


def normalize_price_source(value: Any) -> str:
    raw = _raw(value).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "jup": "jupiter",
        "jupiter_price": "jupiter",
        "jup_batch": "jupiter",
        "jup_single": "jupiter",
        "jup_critical": "jupiter",
        "dex": "dexscreener",
        "dex_full": "dexscreener",
    }
    return aliases.get(raw, raw or "unknown")


def normalize_decision(value: Any, row: Mapping[str, Any] | None = None) -> str:
    row = row or {}
    raw = _raw(value).lower().replace("-", "_").replace(" ", "_")
    reason = _raw(row.get("reason") or row.get("reject_reason") or row.get("delay_reason")).lower()
    stage = _raw(row.get("stage") or row.get("candidate_stage") or row.get("event_type")).lower()
    joined = "|".join([raw, reason, stage])
    # ``live`` is the strategy's requested execution mode, not proof that an
    # order was placed.  Keep it neutral until the row carries execution
    # evidence; this also repairs telemetry written by the legacy contract.
    if raw == "live" and not _has_buy_execution_evidence(row):
        return "observe"
    if "zero_qty" in joined or "execution_blocked" in joined:
        return "execution_blocked"
    if "no_route" in joined:
        return "execution_blocked"
    if raw == "live":
        return "buy"
    if raw in {"buy", "bought", "buy_ok", "paper_buy"}:
        return "buy"
    if not raw and _has_buy_execution_evidence(row):
        return "buy"
    if "shadow" in joined:
        return "shadow"
    if raw in {"wait", "delay", "delayed", "waiting"} or "delay" in joined:
        return "delay"
    if raw in {"reject", "rejected", "policy_reject"} or "reject" in joined or "blocked" in joined:
        return "reject"
    if raw in {"candidate_decision", "candidate_stage"}:
        return "observe"
    return raw if raw in {"buy", "shadow", "reject", "delay", "execution_blocked", "observe"} else "reject"


def normalize_candidate_stage(value: Any, row: Mapping[str, Any] | None = None) -> str:
    row = row or {}
    raw = _raw(value or row.get("stage") or row.get("event_type")).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "candidate_decision": "decision",
        "candidate_outcome": "outcome",
        "candidate_stage": "stage",
        "buy": "execution",
        "bought": "execution",
    }
    return aliases.get(raw, raw or "unknown")


def normalize_outcome(value: Any, row: Mapping[str, Any] | None = None) -> str:
    row = row or {}
    raw = _raw(value).lower().replace("-", "_").replace(" ", "_")
    decision = normalize_decision(_decision_value(row), row)
    raw_action = _raw(row.get("raw_action") or row.get("action")).lower()
    if raw_action == "live" and not _has_buy_execution_evidence(row):
        return "open"
    if raw:
        return raw
    event_type = _raw(row.get("event_type")).lower()
    if event_type == "candidate_outcome" or row.get("pnl_pct") is not None or row.get("label") is not None:
        return "closed"
    if decision == "buy":
        return "bought"
    if decision in {"reject", "delay", "shadow", "execution_blocked"}:
        return decision
    return "open"


def normalize_blocker(value: Any) -> str:
    raw = _raw(value).lower().replace("-", "_").replace(" ", "_")
    if ":" in raw:
        head, _tail = raw.split(":", 1)
        raw = head
    return _BLOCKER_ALIASES.get(raw, raw)


def _extend_blockers(out: list[str], value: Any) -> None:
    if value is None:
        return
    if isinstance(value, (list, tuple, set)):
        items = value
    elif isinstance(value, str):
        items = value.replace(";", ",").split(",")
    else:
        items = [value]
    for item in items:
        normalized = normalize_blocker(item)
        if normalized and normalized not in out:
            out.append(normalized)


def normalize_blockers(row: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    for key in (
        "blockers",
        "reject_reasons",
        "failures",
        "hard_failures",
        "risk_notes",
        "sniper_gate_failures",
        "profit_gate_reject_reasons",
        "blocked_bucket",
        "reason",
    ):
        _extend_blockers(blockers, row.get(key))
    return blockers


def feature_snapshot_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    explicit = row.get("feature_snapshot") or row.get("features_snapshot")
    if isinstance(explicit, Mapping):
        return dict(explicit)
    snapshot: dict[str, Any] = {}
    for key, value in row.items():
        if key in _SNAPSHOT_EXCLUDE_KEYS:
            continue
        if key.startswith("_"):
            continue
        if isinstance(value, (dict, list, tuple, set)):
            continue
        snapshot[str(key)] = value
    return snapshot


def build_candidate_decision_id(row: Mapping[str, Any]) -> str:
    raw = json.dumps(
        {
            "address": row.get("address") or row.get("mint") or row.get("token_address") or "",
            "timestamp": row.get("timestamp") or row.get("ts_utc") or "",
            "lane": row.get("lane") or row.get("entry_lane") or "unknown",
            "decision": row.get("decision") or row.get("decision_action") or row.get("action") or "",
            "reason": row.get("reason") or "",
            "stage": row.get("candidate_stage") or row.get("stage") or "",
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def normalize_candidate_event_row(
    row: Mapping[str, Any],
    *,
    source_file: str | None = None,
    row_index: int | None = None,
) -> dict[str, Any]:
    out = dict(row)
    address = out.get("address") or out.get("mint") or out.get("token_address")
    out["address"] = address
    out["mint"] = out.get("mint") or address
    out["timestamp"] = out.get("timestamp") or out.get("ts_utc")
    action = _raw(out.get("action"))
    if action and not _raw(out.get("raw_action")):
        out["raw_action"] = action
    event_type = _raw(out.get("event_type")).lower().replace("-", "_").replace(" ", "_")
    if event_type == "strategy_decision":
        intent = _raw(out.get("decision_intent") or out.get("raw_action") or out.get("action"))
        if intent:
            out["decision_intent"] = intent
    out["candidate_stage"] = normalize_candidate_stage(out.get("candidate_stage") or out.get("stage"), out)
    out["decision"] = normalize_decision(_decision_value(out), out)
    out["outcome"] = normalize_outcome(out.get("outcome"), out)
    out["entry_regime"] = normalize_entry_regime(out.get("entry_regime") or out.get("regime") or out.get("discovered_via"))
    had_lane = any(_raw(out.get(key)) for key in ("entry_lane", "lane", "profit_lane_tier", "size_bucket"))
    resolved_lane = reconstruct_entry_lane(out)
    out["entry_lane"] = resolved_lane if had_lane or resolved_lane != LANE_UNKNOWN else ""
    out["lane"] = normalize_entry_lane(out.get("lane") or out.get("entry_lane"))
    out["source"] = out.get("source") or out.get("event_type") or out.get("discovered_via") or "unknown"
    out["run_id"] = out.get("run_id") or ""
    out["blockers"] = normalize_blockers(out)
    out["blocker"] = out["blockers"][0] if out["blockers"] else ""
    out["feature_snapshot"] = feature_snapshot_from_row(out)
    out["features_snapshot"] = out["feature_snapshot"]
    out["decision_id"] = out.get("decision_id") or build_candidate_decision_id(out)
    lineage = dict(out.get("row_lineage") or {}) if isinstance(out.get("row_lineage"), Mapping) else {}
    if source_file is not None:
        lineage["source_file"] = str(source_file)
    if row_index is not None:
        lineage["row_index"] = int(row_index)
    if lineage:
        out["row_lineage"] = lineage
    return out


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        if out != out:
            return None
        return out
    except Exception:
        return None


def reconstruct_entry_lane(row: Mapping[str, Any]) -> str:
    explicit = normalize_entry_lane(row.get("entry_lane"))
    if explicit != LANE_UNKNOWN:
        return explicit

    tier = normalize_entry_lane(row.get("profit_lane_tier") or row.get("size_bucket"))
    if tier != LANE_UNKNOWN:
        return tier

    profile = _raw(row.get("gate_profile") or row.get("sniper_gate_profile") or row.get("live_profit_gate_profile")).lower()
    subtype = _raw(row.get("entry_subtype")).lower()
    if subtype == "paper_birth_probe" or profile.startswith("green_sniper_birth_probe"):
        return LANE_PUMP_EARLY_BIRTH_PROBE
    if profile.startswith("late_momentum"):
        return LANE_PUMP_EARLY_LATE_MOMENTUM_WATCH
    if profile.startswith("green_sniper"):
        return LANE_PUMP_EARLY_GREEN_SNIPER
    if profile == "pumpswap_meteor_prime":
        return LANE_PUMP_EARLY_METEOR
    if profile.startswith("pumpswap_breakout"):
        return LANE_PUMP_EARLY_BREAKOUT
    if profile == "pumpswap_profit_prime":
        return LANE_PUMP_EARLY_PRIME
    if profile.startswith("pumpswap_profit"):
        return LANE_PUMP_EARLY_PROFIT
    if profile == "sniper_research_micro_fallback":
        return LANE_SNIPER_RESEARCH_MICRO_FALLBACK
    if profile.startswith("sniper"):
        return LANE_RESEARCH_SNIPER

    dex_id = normalize_dex_id(row.get("dex_id") or row.get("dexId") or row.get("buy_dex_id"))
    regime = normalize_entry_regime(row.get("entry_regime") or row.get("discovered_via"))
    price5m = _to_float(row.get("price_pct_5m") or row.get("buy_price_pct_5m"))
    min_green = _to_float(row.get("green_sniper_min_price_pct_5m")) or 20.0
    if regime == "pump_early" and dex_id == "pumpswap" and price5m is not None and price5m >= min_green:
        return LANE_PUMP_EARLY_GREEN_SNIPER
    if regime == "pump_early" and dex_id == "pumpswap" and row.get("venue_is_pumpswap") in {1, "1", True}:
        return LANE_PUMP_EARLY_PROFIT
    return LANE_UNKNOWN


def normalize_ml_row(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    address = out.get("address") or out.get("mint") or out.get("token_address")
    out["address"] = address
    out["mint"] = out.get("mint") or address
    out["sample_type"] = normalize_sample_type(out.get("sample_type"))
    out["entry_regime"] = normalize_entry_regime(out.get("entry_regime") or out.get("discovered_via"))
    out["entry_lane"] = reconstruct_entry_lane(out)
    out["dex_id"] = normalize_dex_id(out.get("dex_id") or out.get("dexId") or out.get("buy_dex_id"))
    out["price_source"] = normalize_price_source(out.get("price_source") or out.get("price_source_at_buy"))
    if not out.get("gate_profile"):
        out["gate_profile"] = out.get("sniper_gate_profile") or out.get("live_profit_gate_profile") or ""
    if not out.get("profit_lane_tier"):
        out["profit_lane_tier"] = out["entry_lane"] if out["entry_lane"] != LANE_UNKNOWN else ""
    return normalize_candidate_event_row(out)


def apply_data_contract(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    rows = [normalize_ml_row(row) for row in frame.to_dict(orient="records")]
    out = pd.DataFrame(rows)
    for col in REQUIRED_ML_CONTEXT_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA
    return out


def is_live_trade_sample(row: Mapping[str, Any]) -> bool:
    return normalize_sample_type(row.get("sample_type")) == SAMPLE_TRADE_CLOSE


def is_shadow_sample(row: Mapping[str, Any]) -> bool:
    return normalize_sample_type(row.get("sample_type")) in {
        SAMPLE_SHADOW_CLOSE,
        SAMPLE_GREEN_SNIPER_REJECT_SHADOW,
        SAMPLE_LATE_MOMENTUM_WATCH_SHADOW,
        SAMPLE_RESEARCH_RANK_SHADOW,
    }


def is_policy_reject(row: Mapping[str, Any]) -> bool:
    return normalize_sample_type(row.get("sample_type")) == SAMPLE_POLICY_REJECT


def is_execution_blocked_sample(row: Mapping[str, Any]) -> bool:
    return normalize_sample_type(row.get("sample_type")) in {
        SAMPLE_EXECUTION_BLOCKED_NO_ROUTE,
        SAMPLE_EXECUTION_BLOCKED_ZERO_QTY,
    }


def is_productive_training_sample(row: Mapping[str, Any]) -> bool:
    return normalize_sample_type(row.get("sample_type")) in {
        SAMPLE_TRADE_CLOSE,
        SAMPLE_SHADOW_CLOSE,
        SAMPLE_GREEN_SNIPER_REJECT_SHADOW,
        SAMPLE_LATE_MOMENTUM_WATCH_SHADOW,
        SAMPLE_RESEARCH_RANK_SHADOW,
    }


__all__ = [
    "REQUIRED_ML_CONTEXT_COLUMNS",
    "SAMPLE_TRADE_CLOSE",
    "SAMPLE_SHADOW_CLOSE",
    "SAMPLE_POLICY_REJECT",
    "SAMPLE_CANDIDATE",
    "SAMPLE_EXECUTION_BLOCKED_NO_ROUTE",
    "SAMPLE_EXECUTION_BLOCKED_ZERO_QTY",
    "SAMPLE_GREEN_SNIPER_REJECT_SHADOW",
    "SAMPLE_LATE_MOMENTUM_WATCH_SHADOW",
    "SAMPLE_RESEARCH_RANK_SHADOW",
    "SAMPLE_UNKNOWN",
    "VALID_SAMPLE_TYPES",
    "build_candidate_decision_id",
    "feature_snapshot_from_row",
    "normalize_blocker",
    "normalize_blockers",
    "normalize_candidate_event_row",
    "normalize_candidate_stage",
    "normalize_decision",
    "normalize_outcome",
    "normalize_sample_type",
    "normalize_entry_regime",
    "normalize_entry_lane",
    "normalize_dex_id",
    "normalize_price_source",
    "reconstruct_entry_lane",
    "normalize_ml_row",
    "apply_data_contract",
    "is_live_trade_sample",
    "is_shadow_sample",
    "is_policy_reject",
    "is_execution_blocked_sample",
    "is_productive_training_sample",
]
