"""
utils.data_utils
~~~~~~~~~~~~~~~~
• Normaliza el dict-token a claves canónicas y tipos simples.
• is_incomplete() marca tokens sin liquidez o volumen relevante.
• NEW 2025-07-20 :
  – Campos críticos → np.nan cuando faltan (NO 0).
  – fill_provisional_liq_vol(): forward-fill liq/vol en un DataFrame
    y marca como “completo” al tener valores > 0.
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
    "liquidity":      "liquidity_usd",
    "liquidityUsd":   "liquidity_usd",
    "liquidity_usd":  "liquidity_usd",
    # volumen 24 h
    "vol24h":         "volume_24h_usd",
    "vol24h_usd":     "volume_24h_usd",
    "volume24h":      "volume_24h_usd",
    "volume":         "volume_24h_usd",
    "volume_24h":     "volume_24h_usd",
    "volume_24h_usd": "volume_24h_usd",
    # otros
    "holders":        "holders",
    "age_minutes":    "age_minutes",
    "market_cap":     "market_cap_usd",
    "market_cap_usd": "market_cap_usd",
}

_MANDATORY_FLOATS = {"liquidity_usd", "volume_24h_usd"}
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
    Convierte un valor a float.
    • Devuelve **np.nan** si no es convertible (antes 0.0).
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


# ───────── validación externa ───────────────
def is_incomplete(tok: Dict[str, Any]) -> bool:
    """
    True si liquidez o volumen están ausentes (NaN o 0).
    """
    liq = tok.get("liquidity_usd")
    vol = tok.get("volume_24h_usd")
    return (
        (liq is None or (isinstance(liq, float) and np.isnan(liq)) or liq == 0) or
        (vol is None or (isinstance(vol, float) and np.isnan(vol)) or vol == 0)
    )


# ───────── forward-fill retroactivo ─────────
def fill_provisional_liq_vol(df: pd.DataFrame) -> pd.DataFrame:
    """
    Para un DataFrame de un mismo token ordenado por timestamp:
    • forward-fill `liquidity_usd` y `volume_24h_usd`
      (ej.: filas iniciales con NaN se rellenan cuando llegue el dato).
    • Devuelve el df modificado (copy).
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
    • Añade claves faltantes con np.nan.
    """
    clean: Dict[str, Any] = token  # mutación in-place
    ctx = clean.get("symbol") or clean.get("address", "")[:4]

    # 0) si falta created_at → ahora-10 s
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

    # 5) age_minutes None → 0.0
    if clean.get("age_minutes") is None:
        clean["age_minutes"] = 0.0

    # 6) marca de tiempo de la descarga
    clean.setdefault("fetched_at", utc_now())

    return clean


__all__ = [
    "sanitize_token_data",
    "is_incomplete",
    "fill_provisional_liq_vol",
]
