"""Fixed T0 categories: no fitted future vocabulary, ordinal codes or hashes."""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Iterable
from types import MappingProxyType

import numpy as np
import pandas as pd

from ml.lane_taxonomy import TRAINABLE_LANES, normalize_entry_lane
from features.numeric_encoding import FEATURE_SOURCES as NUMERIC_SOURCES
from features.strategy_context import (SOURCE as STRATEGY_SOURCE, VALUES as STRATEGY_VALUES,
    ENTRY_VERSION as STRATEGY_ENTRY_VERSION, checked_population, context_values)

VERSION = "fixed_t0_context_onehot_v1"
PREFIX = "t0ctx_"
MISSING = "unobserved"
OTHER = "unrecognized"

# This is a software vocabulary, not a vocabulary learned from outcome rows.
# Unrecognized future values remain a distinct 'other', never a known lane.
DOMAINS: dict[str, tuple[str, ...]] = {
    "entry_lane": tuple(sorted(TRAINABLE_LANES)),
    "gate_profile": (
        "birth_probe_micro_canary", "green_sniper", "green_sniper_birth_probe",
        "green_sniper_restricted_buy", "late_momentum_watch", "moonshot_micro_lottery",
        "paper_bootstrap", "paper_exploration_quota", "pumpswap_breakout_probe",
        "pumpswap_meteor_prime", "pumpswap_profit_broad", "pumpswap_profit_prime",
        "pumpswap_rebound_prime", "pumpswap_precision", "pumpswap_profit_research",
        "paper_aggressive_research_guard", "paper_aggressive_research_buy",
        "live_aggressive_research_guard", "live_aggressive_research_buy",
        "research_rank_canary", "shadow_followup_micro",
        "sniper_core", "sniper_hot", "sniper_micro", "sniper_research_deep_reversal",
        "sniper_research_micro_fallback", "sniper_research_momentum_ignition",
    ),
    "profit_lane_tier": tuple(sorted(TRAINABLE_LANES | {
        "micro", "core", "hot", "broad", "prime", "meteor", "research",
        "pumpswap_rebound_prime_shadow", "pumpswap_prime_strict_blocked",
        "pump_early_pumpswap_precision", "paper_aggressive", "live_aggressive",
    })),
    "exit_profile": (
        "bird_runner", "broad_runner", "green_sniper_runner", "jackpot_runner",
        "meteor_runner", "prime_runner",
    ),
    "social_status": ("present", "missing", "unknown", "suspicious"),
    "green_sniper_risk_level": ("low", "medium", "high", "critical", "lethal", "unknown"),
    "liquidity_risk_level": ("low", "medium", "high", "critical", "lethal", "unknown"),
}
# Reserve missing/other exactly once even if a software category has that name.
DOMAINS = {source: tuple(dict.fromkeys((*values, MISSING, OTHER))) for source, values in DOMAINS.items()}
FEATURE_SOURCES = {f"{PREFIX}{source}__{value}": source
    for source, values in DOMAINS.items() for value in values}
SCHEMA_SHA256 = sha256(json.dumps(DOMAINS, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
# Preserve the exact v1 vocabulary/hash for every previously approved feature list.
STRATEGY_VERSION = "fixed_t0_context_onehot_with_selected_subprofile_v2"
DOMAINS[STRATEGY_SOURCE] = (*STRATEGY_VALUES, MISSING, OTHER)
STRATEGY_SCHEMA_SHA256 = sha256(json.dumps({"domains": DOMAINS,
    "entry_receipt_version": STRATEGY_ENTRY_VERSION}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
FEATURE_SOURCES.update({f"{PREFIX}{STRATEGY_SOURCE}__{value}": STRATEGY_SOURCE
                       for value in DOMAINS[STRATEGY_SOURCE]})
CONTEXT_FEATURES = tuple(FEATURE_SOURCES)
DOMAINS = MappingProxyType(DOMAINS)
FEATURE_SOURCES = MappingProxyType(FEATURE_SOURCES)


def category_key(value: Any, source: str) -> str:
    if value is None or not isinstance(value, (str, int, float, bool, np.generic)):
        return MISSING
    try:
        if pd.isna(value):
            return MISSING
    except (TypeError, ValueError):
        return MISSING
    raw = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if not raw or raw in {"none", "nan", "<na>", "null"}:
        return MISSING
    if source == "entry_lane":
        raw = normalize_entry_lane(raw)
    return raw if raw in DOMAINS[source] else OTHER


def encode_context_row(row: dict[str, Any]) -> dict[str, int]:
    keys = {source: category_key(row.get(source), source) for source in DOMAINS}
    return {feature: int(feature == f"{PREFIX}{source}__{keys[source]}")
            for feature, source in FEATURE_SOURCES.items()}


def available_context_features(frame: pd.DataFrame) -> list[str]:
    """Training cannot create apparent input evidence from absent descriptors."""
    _, proofs = context_values(frame) if STRATEGY_SOURCE in frame else ([], [])
    original_strategy = bool(proofs) and all(proof is not None for proof in proofs)
    return [name for name, source in FEATURE_SOURCES.items() if source in frame
            and (source != STRATEGY_SOURCE or original_strategy)]


def augment_context_frame(frame: pd.DataFrame, features: Iterable[str] | None = None) -> pd.DataFrame:
    """Derive from raw T0 descriptors, never trust stale precomputed indicators."""
    requested = list(CONTEXT_FEATURES if features is None else features)
    requested = [name for name in requested if name in FEATURE_SOURCES]
    out = frame.copy()
    additions = {}
    for source in dict.fromkeys(FEATURE_SOURCES[name] for name in requested):
        raw = (pd.Series(context_values(frame)[0], index=frame.index, dtype=object)
               if source == STRATEGY_SOURCE else frame[source] if source in frame
               else pd.Series(None, index=frame.index, dtype=object))
        keys = raw.map(lambda value: category_key(value, source)).to_numpy()
        for name in requested:
            if FEATURE_SOURCES[name] == source:
                additions[name] = (keys == name.split("__", 1)[1]).astype(np.float32)
    if not additions:
        return out
    out = out.drop(columns=[name for name in additions if name in out])
    return pd.concat([out, pd.DataFrame(additions, index=frame.index)], axis=1)


def context_encoding_schema(features: Iterable[str]) -> dict[str, Any] | None:
    encoded = [name for name in features if name.startswith(PREFIX)]
    if not encoded:
        return None
    if any(name not in FEATURE_SOURCES for name in encoded):
        raise ValueError("unknown T0 context feature")
    if any(FEATURE_SOURCES[name] == STRATEGY_SOURCE for name in encoded):
        return {"version": STRATEGY_VERSION, "schema_sha256": STRATEGY_SCHEMA_SHA256,
                "entry_receipt_version": STRATEGY_ENTRY_VERSION, "encoded_features": encoded}
    return {"version": VERSION, "schema_sha256": SCHEMA_SHA256, "encoded_features": encoded}


def checked_context_schema(metadata: dict[str, Any], features: Iterable[str]) -> bool:
    try:
        expected = context_encoding_schema(features)
        return (metadata.get("context_encoding") == expected
                and (expected is None or expected["version"] == VERSION or checked_population(metadata)))
    except (TypeError, ValueError, AttributeError):
        return False


def independent_input_count(features: Iterable[str]) -> int:
    return len({FEATURE_SOURCES.get(name, NUMERIC_SOURCES.get(name, name)) for name in features})
