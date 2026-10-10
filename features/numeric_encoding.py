"""Fixed T0 numeric validity and missingness, derived only from raw inputs."""
from __future__ import annotations

from hashlib import sha256
import json
import math
from types import MappingProxyType
from typing import Iterable

import numpy as np
import pandas as pd
from utils.numeric_types import binary_value
from analytics.token_time import AGE_SEMANTICS_VERSION

VERSION = "typed_t0_numeric_missingness_v1"
PREFIX = "t0num_missing__"
_FLAGS = "cluster_bad mint_auth_renounced impact_zero_flag social_ok twitter_present telegram_present discord_present website_present has_jupiter_route require_jupiter_for_buy route_proxy liquidity_is_proxy venue_is_pumpswap green_sniper_paper_birth_probe missing_liquidity missing_volume missing_holders missing_rug_score missing_socials missing_trend".split()
_COUNTS = "queue_attempts snapshot_missing_fields coverage_core_fields txns_last_5m txns_last_5m_buys txns_last_5m_sells holders rug_score social_link_count social_latency_ms twitter_followers discord_members discovered_via_code entry_regime_code dex_id_code price_source_quality price5m_bucket_code mcap_bucket_code score_total".split()
_CONTINUOUS = "age_minutes queue_age_minutes liquidity_usd volume_24h_usd market_cap_usd price_pct_1m price_pct_5m green_sniper_score volume_pct_5m price_impact_pct social_confidence_bonus".split()
RULES = {**{name: "binary" for name in _FLAGS}, **{name: "count_int32" for name in _COUNTS},
         **{name: "finite_float32" for name in _CONTINUOUS}, "trend": "signed_trend"}
FEATURE_SOURCES = {PREFIX + name: name for name in RULES}
MISSINGNESS_FEATURES = tuple(FEATURE_SOURCES)
SCHEMA_SHA256 = sha256(json.dumps(RULES, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
RULES, FEATURE_SOURCES = MappingProxyType(RULES), MappingProxyType(FEATURE_SOURCES)


def numeric_value(value, source: str):
    """Same scalar contract for one-row inference and batch training."""
    rule = RULES[source]
    if rule == "binary":
        return binary_value(value)
    if isinstance(value, (bool, np.bool_, dict, list, tuple, set, np.ndarray, complex, np.complexfloating)): return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number) or abs(number) > np.finfo(np.float32).max: return None
    if rule == "count_int32":
        if number < 0 or number > np.iinfo(np.int32).max or not number.is_integer(): return None
    elif rule == "signed_trend":
        if number not in (-1., 0., 1.): return None
    return number


def numeric_values(raw: pd.Series, source: str) -> pd.Series:
    """No return/momentum ceiling: only representability and declared types."""
    # Keep decoded raw precision until the final matrix conversion. Rounding
    # int32-max to float32 here would make a second encoding mark it invalid.
    return raw.map(lambda value: numeric_value(value, source)).astype(np.float64)


def available_numeric_features(frame: pd.DataFrame) -> list[str]:
    return [name for name, source in FEATURE_SOURCES.items() if source in frame]


def augment_numeric_frame(frame: pd.DataFrame, features: Iterable[str]) -> pd.DataFrame:
    requested = [name for name in features if name in FEATURE_SOURCES]
    if not requested: return frame.copy()
    out, additions = frame.copy(), {}
    # Read each source in its original column dtype. A row Series can coerce
    # otherwise valid real inputs to complex because of an unrelated column.
    single = ({source: frame[source].iat[0] if source in frame else None
               for source in dict.fromkeys(FEATURE_SOURCES[name] for name in requested)}
              if len(frame) == 1 else None)
    for name in dict.fromkeys(requested):
        source = FEATURE_SOURCES[name]
        if single is not None:
            value = numeric_value(single.get(source), source)
            additions[source] = np.asarray([np.nan if value is None else value], dtype=np.float64)
            additions[name] = np.asarray([int(value is None)], dtype=np.float32)
        else:
            raw = frame[source] if source in frame else pd.Series(None, index=frame.index, dtype=object)
            values = numeric_values(raw, source)
            additions[source], additions[name] = values, values.isna().astype(np.float32)
    # Recompute indicators: a caller cannot supply a stale/forged availability.
    out = out.drop(columns=[name for name in additions if name in out])
    return pd.concat([out, pd.DataFrame(additions, index=frame.index)], axis=1)


def numeric_encoding_schema(features: Iterable[str]) -> dict | None:
    names = list(features)
    encoded = [name for name in names if name.startswith(PREFIX)]
    age_used = "age_minutes" in names or "queue_age_minutes" in names
    if not encoded:
        if not age_used: return None  # Unrelated legacy matrix semantics are unchanged.
        return {"token_clock_semantics":AGE_SEMANTICS_VERSION,
                "imputation":"unchanged_legacy_matrix", "encoded_features":[]}
    if (any(name not in FEATURE_SOURCES for name in encoded)
            or any(FEATURE_SOURCES[name] not in names for name in encoded)
            or any(PREFIX + name not in names for name in names if name in RULES)):
        raise ValueError("Numeric missingness requires known paired raw inputs")
    schema = {"version": VERSION, "schema_sha256": SCHEMA_SHA256, "encoded_features": encoded,
              "imputation": "zero_with_explicit_missing_indicator", "invalid_numeric": "unobserved"}
    if age_used:
        schema["token_clock_semantics"] = AGE_SEMANTICS_VERSION
    return schema


def checked_numeric_schema(metadata, features) -> bool:
    try:
        return metadata.get("numeric_encoding") == numeric_encoding_schema(features)
    except (AttributeError, TypeError, ValueError):
        return False
