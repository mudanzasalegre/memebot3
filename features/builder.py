"""
features.builder
~~~~~~~~~~~~~~~~
Convierte el dict-token en un vector listo para el modelo LightGBM
y añade un flag `is_incomplete` (0/1) cuando faltan métricas clave.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict

import pandas as pd

from utils.data_utils import sanitize_token_data
from utils.time import utc_now

# ─────────────────────────── columnas ────────────────────────────
COLUMNS: list[str] = [
    # meta
    "address",
    "timestamp",
    "discovered_via",
    # liquidez / actividad
    "age_minutes",
    "liquidity_usd",
    "volume_24h_usd",
    "txns_last_5m",
    "holders",
    # riesgo
    "rug_score",
    "cluster_bad",
    "mint_auth_renounced",
    # momentum
    "price_pct_1m",
    "price_pct_5m",
    "volume_pct_5m",
    # social
    "social_ok",
    "twitter_followers",
    "discord_members",
    # señales internas
    "score_total",
    "trend",
    # —— NUEVO flag de completitud ——  (no se usa en el modelo)
    "is_incomplete",
]

_BOOL_COLS = {
    "cluster_bad",
    "mint_auth_renounced",
    "social_ok",
}

_CRITICAL = ("liquidity_usd", "volume_24h_usd")   # para el flag

# ────────────────────────── builder ─────────────────────────────
def build_feature_vector(tok: Dict[str, Any]) -> pd.Series:
    """
    Parameters
    ----------
    tok : dict crudo – se sanitiza internamente.

    Returns
    -------
    pd.Series con índice=COLUMNS
    """
    tok = sanitize_token_data(tok)

    now = utc_now()
    age_min = (now.replace(tzinfo=dt.timezone.utc) - tok["created_at"]).total_seconds() / 60.0

    # base obligatoria
    values: Dict[str, Any] = {
        "address":        tok["address"],
        "timestamp":      now,
        "discovered_via": tok.get("discovered_via", "dex"),
        "age_minutes":    age_min,
    }

    # resto de campos
    for col in COLUMNS:
        if col in values:
            continue
        if col == "is_incomplete":               # se calcula al final
            continue
        val = tok.get(col, 0)
        if col in _BOOL_COLS:
            val = int(bool(val))
        if val is None:
            val = 0
        values[col] = val

    # —— flag is_incomplete ——————————————————————————————
    values["is_incomplete"] = int(
        any(values.get(k, 0) == 0 for k in _CRITICAL)
    )

    # Devuelve en el orden estricto de COLUMNS
    return pd.Series([values[c] for c in COLUMNS], index=COLUMNS)
