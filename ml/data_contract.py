from __future__ import annotations

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
    "entry_regime",
    "entry_lane",
    "gate_profile",
    "profit_lane_tier",
    "dex_id",
    "price_source",
    "label",
    "target_total_pnl_pct",
)


def _raw(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


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
    return out


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
    return normalize_sample_type(row.get("sample_type")) == SAMPLE_SHADOW_CLOSE


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
