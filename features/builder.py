# memebot3/features/builder.py
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterable

import numpy as np
import pandas as pd

from utils.data_utils import sanitize_token_data
from utils.time import utc_now

COLUMNS: list[str] = [
    "address",
    "timestamp",
    "discovered_via",
    "age_minutes",
    "liquidity_usd",
    "volume_24h_usd",
    "market_cap_usd",
    "txns_last_5m",
    "holders",
    "rug_score",
    "cluster_bad",
    "mint_auth_renounced",
    "price_pct_1m",
    "price_pct_5m",
    "volume_pct_5m",
    "social_ok",
    "twitter_followers",
    "discord_members",
    "score_total",
    "trend",
    "is_incomplete",
]

_BOOL_COLS = {"cluster_bad", "mint_auth_renounced", "social_ok"}
_CRITICAL = ("liquidity_usd", "volume_24h_usd")

# Control conceptual T0
ALLOWED_FEATURES: set[str] = {
    "discovered_via",
    "age_minutes",
    "liquidity_usd",
    "volume_24h_usd",
    "market_cap_usd",
    "txns_last_5m",
    "holders",
    "rug_score",
    "cluster_bad",
    "mint_auth_renounced",
    "price_pct_1m",
    "price_pct_5m",
    "volume_pct_5m",
    "social_ok",
    "twitter_followers",
    "discord_members",
    "score_total",
    "trend",
}
FORBIDDEN_FEATURES: set[str] = set()
_FORBIDDEN_SUBSTR: tuple[str, ...] = (
    "pnl",
    "close_price",
    "_at_close",
    "_after_",
    "outcome",
    "result",
    "sell",
    "exit",
    "tp_",
    "sl_",
)

def _has_forbidden_keys(d: Dict[str, Any], forbidden_exact: Iterable[str], forbidden_substr: Iterable[str]) -> list[str]:
    keys = []
    for k in d.keys():
        lk = str(k).lower()
        if k in forbidden_exact:
            keys.append(k)
            continue
        if any(sub in lk for sub in forbidden_substr):
            keys.append(k)
    return keys

def build_feature_vector(tok: Dict[str, Any]) -> pd.Series:
    tok = sanitize_token_data(tok)

    bad_keys = _has_forbidden_keys(tok, FORBIDDEN_FEATURES, _FORBIDDEN_SUBSTR)
    assert not bad_keys, f"Token incluye claves de futuro/no permitidas: {bad_keys}"

    now = utc_now()
    created_at = tok.get("created_at")
    if created_at is None:
        raise ValueError("Falta created_at en token")
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.timezone.utc)
    age_min = (now - created_at).total_seconds() / 60.0

    values: Dict[str, Any] = {
        "address": tok.get("address"),
        "timestamp": now,
        "discovered_via": tok.get("discovered_via", "dex"),
        "age_minutes": age_min,
    }

    for col in COLUMNS:
        if col in values or col == "is_incomplete":
            continue
        val = tok.get(col, None)
        if col in _BOOL_COLS:
            val = int(bool(val))
        elif val is None:
            val = np.nan
        values[col] = val

    values["is_incomplete"] = int(
        any(pd.isna(values[k]) or values[k] == 0 for k in _CRITICAL if k in values)
    )

    return pd.Series([values.get(c, np.nan) for c in COLUMNS], index=COLUMNS)
