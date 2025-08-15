# memebot3/utils/data_utils.py
"""
Normaliza el dict-token a claves canónicas y tipos simples.

Cambios 2025-08-02
──────────────────
• Alias GeckoTerminal → canónicos.
• Se garantiza que los campos críticos (liq, vol, mcap) NUNCA queden NaN/None.

Cambios 2025-08-15
──────────────────
• Saneo temprano de direcciones: strip sufijo 'pump' y validación ligera de mint SPL.
  (Afecta a token["address"], token_address/tokenAddress, baseToken.address, poolAddress)
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Dict

import numpy as np
import pandas as pd

from utils.time import utc_now
from utils.solana_addr import normalize_mint, is_probably_mint

# Usamos un logger estable para estos saneos
log = logging.getLogger("data")

# ───────── alias brutos → canónicos ──────────
_NUMERIC_ALIASES: dict[str, str] = {
    # liquidez
    "liquidity":       "liquidity_usd",
    "liquidityUsd":    "liquidity_usd",
    "liquidity_usd":   "liquidity_usd",
    "liq_usd":         "liquidity_usd",      # GeckoTerminal
    # volumen 24 h
    "vol24h":          "volume_24h_usd",
    "vol24h_usd":      "volume_24h_usd",
    "volume24h":       "volume_24h_usd",
    "volume":          "volume_24h_usd",
    "volume_24h":      "volume_24h_usd",
    "volume_24h_usd":  "volume_24h_usd",
    "volume_usd":      "volume_24h_usd",     # GeckoTerminal
    # market-cap
    "market_cap":      "market_cap_usd",
    "market_cap_usd":  "market_cap_usd",
    "mcap":            "market_cap_usd",     # GeckoTerminal
    # otros
    "holders":         "holders",
    "age_minutes":     "age_minutes",
    "age_min":         "age_minutes",
}

# Campos FLOAT NOT NULL en la base de datos
_MANDATORY_FLOATS = {"liquidity_usd", "volume_24h_usd", "market_cap_usd"}
# Campos INT NOT NULL en la base de datos
_INT_NOT_NULL = ("holders", "txns_last_5m")

_TREND_STR_TO_INT = {
    "up": 1, "uptrend": 1, "bull": 1, "bullish": 1,
    "down": -1, "downtrend": -1, "bear": -1, "bearish": -1,
    "flat": 0, "sideways": 0, "neutral": 0, "unknown": 0,
}
_PREF_KEYS = ("usd", "h24", "24h", "quote", "base", "value")


# ───────── saneo de direcciones (mint SPL) ─────────
def _sanitize_address_inplace(token: dict) -> None:
    """
    Normaliza direcciones candidatas a mint SPL en el dict token.
    - token["address"]
    - token.get("token_address"), token.get("tokenAddress")
    - token.get("baseToken", {}).get("address")
    - token.get("poolAddress")
    """
    candidates: list[tuple[str, str, str]] = []

    # Campos planos
    for key in ("address", "token_address", "tokenAddress", "poolAddress"):
        val = token.get(key)
        if isinstance(val, str):
            candidates.append(("root", key, val))

    # Campos anidados: baseToken.address
    base = token.get("baseToken")
    if isinstance(base, dict):
        baddr = base.get("address")
        if isinstance(baddr, str):
            candidates.append(("baseToken", "address", baddr))

    picked = None
    for scope, key, raw in candidates:
        cleaned = normalize_mint(raw)
        if cleaned:
            if scope == "root":
                token[key] = cleaned
            else:  # scope == "baseToken"
                try:
                    token["baseToken"]["address"] = cleaned
                except Exception:
                    pass
            if picked is None:
                picked = cleaned

    # Garantiza que token["address"] refleje el mint válido detectado
    if picked:
        if token.get("address") != picked:
            try:
                log.debug("[data] address normalizado %r → %r", token.get("address"), picked)
            except Exception:
                pass
            token["address"] = picked
    else:
        # Si había address pero no parece mint, anotamos en DEBUG
        addr = token.get("address")
        if isinstance(addr, str) and not is_probably_mint(addr):
            try:
                log.debug("[data] address no parece mint SPL: %r", addr)
            except Exception:
                pass


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
    if not ts:
        return np.nan
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return (utc_now() - ts).total_seconds() / 60.0


# ───────── validación externa ───────────────
def is_incomplete(tok: Dict[str, Any]) -> bool:
    """True si faltan métricas críticas (liq, vol, holders)."""
    liq, vol, holders = tok.get("liquidity_usd"), tok.get("volume_24h_usd"), tok.get("holders")
    missing = lambda x: x in (None, 0) or (isinstance(x, float) and math.isnan(x))
    return missing(liq) or missing(vol) or missing(holders)


# ───────── forward-fill retroactivo ─────────
def fill_provisional_liq_vol(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().sort_values("timestamp")
    for col in ("liquidity_usd", "volume_24h_usd"):
        df[col] = df[col].fillna(method="ffill")
    return df


# ───────── función principal ────────────────
def sanitize_token_data(token: Dict[str, Any]) -> Dict[str, Any]:
    """
    • Aplica alias → claves canónicas.
    • Convierte numéricos a float; NaN en campos NOT NULL → 0.0.
    • Normaliza booleanos, trend, edad y marcas temporales.
    • **NUEVO**: sanea direcciones candidatas (quita sufijo 'pump' y valida mint).
    """
    clean = token  # mutación in-place
    ctx   = clean.get("symbol") or clean.get("address", "")[:4]

    # -1) saneo temprano de mint/poolAddress/baseToken.address
    try:
        _sanitize_address_inplace(clean)
    except Exception:
        # best-effort, nunca interrumpir flujo por el saneo
        pass

    # 0) created_at faltante → ahora-10 s
    clean.setdefault("created_at", utc_now() - dt.timedelta(seconds=10))

    # 1) alias → canónico + cast numérico
    for raw, canon in list(_NUMERIC_ALIASES.items()):
        if raw in clean:
            clean[canon] = _to_float(clean.pop(raw), ctx)

    # 2) placeholders para FLOAT NOT NULL
    for fld in _MANDATORY_FLOATS:
        clean.setdefault(fld, np.nan)

    # 3) booleans → int
    for b in ("cluster_bad", "social_ok"):
        if b in clean:
            clean[b] = int(bool(clean[b]))

    # 4) trend a -1 / 0 / 1
    if "trend" in clean:
        clean["trend"] = _normalize_trend(clean["trend"])

    # 5) edad en minutos
    age_val = _minutes_since(clean.get("created_at"))
    clean["age_minutes"] = clean["age_min"] = age_val

    # 6) FLOAT NOT NULL: NaN/None → 0.0
    for fld in _MANDATORY_FLOATS:
        val = clean.get(fld)
        if val is None or (isinstance(val, float) and math.isnan(val)):
            clean[fld] = 0.0

    # 7) INT NOT NULL: NaN/None → 0
    for fld in _INT_NOT_NULL:
        val = clean.get(fld)
        try:
            clean[fld] = 0 if val in (None, np.nan) else int(val)
        except Exception:  # valores raros
            clean[fld] = 0

    # 8) marca de tiempo descarga
    clean.setdefault("fetched_at", utc_now())
    return clean


# ───────── valores por defecto opcionales ───
DEFAULTS = {
    "liquidity_usd"  : 0.0,
    "volume_24h_usd" : 0.0,
    "market_cap_usd" : 0.0,
    "holders"        : 0,
    "rug_score"      : 0,
    "cluster_bad"    : 0,
    "social_ok"      : 0,
    "trend"          : 0.0,
    "insider_sig"    : 0,
    "score_total"    : 0,
}

def apply_default_values(tok: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in DEFAULTS.items():
        tok.setdefault(k, v)
    return tok


__all__ = [
    "sanitize_token_data",
    "is_incomplete",
    "fill_provisional_liq_vol",
    "apply_default_values",
]
