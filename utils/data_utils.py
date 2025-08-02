# memebot3/utils/data_utils.py
"""
utils.data_utils
~~~~~~~~~~~~~~~~
• Normaliza el dict-token a claves canónicas y tipos simples.
• is_incomplete() marca tokens sin liquidez o volumen relevante.

Cambios 2025-08-02
──────────────────
✔  Se añaden alias procedentes de GeckoTerminal:
   • liq_usd  → liquidity_usd
   • volume_usd → volume_24h_usd
   • mcap     → market_cap_usd
✔  El resto del flujo (age_min, casts, etc.) no se toca.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict

import numpy as np
import pandas as pd

from utils.time import utc_now

log = logging.getLogger(__name__)

# ───────── alias brutos → canónicos ──────────
_NUMERIC_ALIASES: dict[str, str] = {
    # liquidez
    "liquidity":       "liquidity_usd",
    "liquidityUsd":    "liquidity_usd",
    "liquidity_usd":   "liquidity_usd",
    "liq_usd":         "liquidity_usd",      # ← GeckoTerminal
    # volumen 24 h
    "vol24h":          "volume_24h_usd",
    "vol24h_usd":      "volume_24h_usd",
    "volume24h":       "volume_24h_usd",
    "volume":          "volume_24h_usd",
    "volume_24h":      "volume_24h_usd",
    "volume_24h_usd":  "volume_24h_usd",
    "volume_usd":      "volume_24h_usd",     # ← GeckoTerminal
    # market-cap
    "market_cap":      "market_cap_usd",
    "market_cap_usd":  "market_cap_usd",
    "mcap":            "market_cap_usd",     # ← GeckoTerminal
    # otros
    "holders":         "holders",
    "age_minutes":     "age_minutes",
    "age_min":         "age_minutes",        # alias nuevo
}

_MANDATORY_FLOATS = {"liquidity_usd", "volume_24h_usd"}
# Campos INT que en la BD son NOT NULL ⇒ jamás se guardan como NaN/None
_INT_NOT_NULL = ("holders", "txns_last_5m")

_TREND_STR_TO_INT = {
    "up": 1, "uptrend": 1, "bull": 1, "bullish": 1,
    "down": -1, "downtrend": -1, "bear": -1, "bearish": -1,
    "flat": 0, "sideways": 0, "neutral": 0, "unknown": 0,
}
_PREF_KEYS = ("usd", "h24", "24h", "quote", "base", "value")


# ───────── helpers numéricos ─────────────────
def _extract_from_dict(d: dict, ctx: str) -> float | None:
    for k in _PREF_KEYS:
        if k in d:
            return _to_float(d[k], ctx)
    for v in d.values():
        num = _to_float(v, ctx)
        if num is not None:
            return num
    return None


def _to_float(value: Any, ctx: str = "") -> float | None:
    """
    Convierte *value* a float.
    • Devuelve **np.nan** si no es convertible.
    """
    if value is None:
        return np.nan
    if isinstance(value, dict):
        return _extract_from_dict(value, ctx)
    if isinstance(value, (list, tuple)) and value:
        return _to_float(value[0], ctx)
    try:
        return float(value)
    except (ValueError, TypeError):
        log.debug("No convertible a float [%s] → %s (%s)",
                  ctx, value, type(value).__name__)
        return np.nan


def _normalize_trend(v: Any) -> int:
    if isinstance(v, (int, float)):
        return int(max(min(v, 1), -1))
    if isinstance(v, str):
        return _TREND_STR_TO_INT.get(v.lower().strip(), 0)
    return 0


def _minutes_since(ts: dt.datetime | None) -> float:
    """Devuelve minutos transcurridos desde *ts* o np.nan si ts es None."""
    if not ts:
        return np.nan
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return (utc_now() - ts).total_seconds() / 60.0


# ───────── validación externa ───────────────
def is_incomplete(tok: Dict[str, Any]) -> bool:
    """True si faltan métricas **críticas** (liq, vol, holders)."""
    liq = tok.get("liquidity_usd")
    vol = tok.get("volume_24h_usd")
    holders = tok.get("holders")

    missing_liq = liq is None or (isinstance(liq, float) and np.isnan(liq)) or liq == 0
    missing_vol = vol is None or (isinstance(vol, float) and np.isnan(vol)) or vol == 0
    missing_hol = holders is None or (isinstance(holders, float) and np.isnan(holders)) or holders == 0

    return missing_liq or missing_vol or missing_hol


# ───────── forward-fill retroactivo ─────────
def fill_provisional_liq_vol(df: pd.DataFrame) -> pd.DataFrame:
    """
    Para un DataFrame de un mismo token ordenado por timestamp:
    • forward-fill `liquidity_usd` y `volume_24h_usd`.
    """
    df = df.copy().sort_values("timestamp")
    for col in ("liquidity_usd", "volume_24h_usd"):
        df[col] = df[col].fillna(method="ffill")
    return df


# ───────── función principal ────────────────
def sanitize_token_data(token: Dict[str, Any]) -> Dict[str, Any]:
    """
    • Alias → nombres canónicos.  
    • Castea numéricos vía _to_float (→ float o np.nan).  
    • Asegura tipos simples y valores válidos para la BD.
    """
    clean: Dict[str, Any] = token        # mutación in-place
    ctx = clean.get("symbol") or clean.get("address", "")[:4]

    # 0) created_at inexistente → ahora-10 s (evita age negativa futura)
    if not clean.get("created_at"):
        clean["created_at"] = utc_now() - dt.timedelta(seconds=10)

    # 1) alias → canónico + cast numérico
    for raw, canon in list(_NUMERIC_ALIASES.items()):
        if raw in clean:
            clean[canon] = _to_float(clean.pop(raw), ctx)

    # 2) campos críticos garantizados (np.nan por defecto)
    for fld in _MANDATORY_FLOATS:
        clean.setdefault(fld, np.nan)

    # 3) booleans → int
    for b in ("cluster_bad", "social_ok"):
        if b in clean:
            clean[b] = int(bool(clean[b]))

    # 4) trend
    if "trend" in clean:
        clean["trend"] = _normalize_trend(clean["trend"])

    # 5) edad en minutos ————
    age_val = _minutes_since(clean.get("created_at"))
    clean["age_minutes"] = age_val
    clean["age_min"]     = age_val

    # 6) ints NOT-NULL nunca deben ser NaN/None
    for fld in _INT_NOT_NULL:
        val = clean.get(fld)
        if val is None or (isinstance(val, float) and np.isnan(val)):
            clean[fld] = 0      # default
        else:
            try:
                clean[fld] = int(val)
            except Exception:
                clean[fld] = 0

    # 7) marca de tiempo de la descarga
    clean.setdefault("fetched_at", utc_now())

    return clean


# ───────── valores por defecto opcionales ───
DEFAULTS = {
    "rug_score": 0.5,
    "twitter_followers": 0,
    "discord_members": 0,
    "insider_sig": False,
}


def apply_default_values(tok: Dict[str, Any]) -> Dict[str, Any]:
    """Rellena métricas opcionales ausentes con valores por defecto."""
    for k, v in DEFAULTS.items():
        tok.setdefault(k, v)
    return tok


__all__ = [
    "sanitize_token_data",
    "is_incomplete",
    "fill_provisional_liq_vol",
    "apply_default_values",
]
