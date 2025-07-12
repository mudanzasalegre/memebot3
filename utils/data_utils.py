"""
utils.data_utils
~~~~~~~~~~~~~~~~
• Normaliza el dict-token a claves canónicas y tipos simples.
• is_incomplete() marca tokens sin liquidez o volumen relevante.

2025-07-13
──────────
• NEW : si falta «created_at» se rellena con utc_now() –10 s
  para evitar TypeError en age_minutes.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict

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
        if num:
            return num
    return None

def _to_float(value: Any, ctx: str = "") -> float:
    if value is None:
        return 0.0
    if isinstance(value, dict):
        maybe = _extract_from_dict(value, ctx)
        return maybe if maybe is not None else 0.0
    if isinstance(value, (list, tuple)) and value:
        return _to_float(value[0], ctx)
    try:
        return float(value)
    except (ValueError, TypeError):
        log.debug("No convertible a float [%s] → %s (%s)",
                  ctx, value, type(value).__name__)
        return 0.0

def _normalize_trend(v: Any) -> int:
    if isinstance(v, (int, float)):
        return int(max(min(v, 1), -1))
    if isinstance(v, str):
        return _TREND_STR_TO_INT.get(v.lower().strip(), 0)
    return 0

# ───────── validación externa ───────────────
def is_incomplete(tok: Dict[str, Any]) -> bool:
    return not tok.get("liquidity_usd") or not tok.get("volume_24h_usd")

# ───────── función principal ────────────────
def sanitize_token_data(token: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = token           # mutación in-place
    ctx = clean.get("symbol") or clean.get("address", "")[:4]

    # 0) si falta created_at → ahora-10 s
    if not clean.get("created_at"):
        clean["created_at"] = utc_now() - dt.timedelta(seconds=10)

    # 1) alias → canónico + cast numérico
    for raw, canon in list(_NUMERIC_ALIASES.items()):
        if raw in clean:
            clean[canon] = _to_float(clean.pop(raw), ctx)

    # 2) campos críticos garantizados
    for fld in _MANDATORY_FLOATS:
        clean.setdefault(fld, 0.0)

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

__all__ = ["sanitize_token_data", "is_incomplete"]
