from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Dict

import pandas as pd

from utils.solana_addr import is_probably_mint, normalize_mint
from utils.time import utc_now, parse_iso_utc

log = logging.getLogger("data")

_NUMERIC_ALIASES: dict[str, str] = {
    "liquidity": "liquidity_usd",
    "liquidityUsd": "liquidity_usd",
    "liquidity_usd": "liquidity_usd",
    "liq_usd": "liquidity_usd",
    "vol24h": "volume_24h_usd",
    "vol24h_usd": "volume_24h_usd",
    "volume24h": "volume_24h_usd",
    "volume": "volume_24h_usd",
    "volume_24h": "volume_24h_usd",
    "volume_24h_usd": "volume_24h_usd",
    "volume_usd": "volume_24h_usd",
    "market_cap": "market_cap_usd",
    "market_cap_usd": "market_cap_usd",
    "mcap": "market_cap_usd",
    "holders": "holders",
    "age_minutes": "age_minutes",
    "age_min": "age_minutes",
}

_FLOAT_FIELDS = {
    "liquidity_usd",
    "volume_24h_usd",
    "market_cap_usd",
    "price_usd",
    "price_native",
    "age_minutes",
    "age_min",
    "price_pct_1m",
    "price_pct_5m",
    "volume_pct_5m",
}
_INT_FIELDS = {
    "holders",
    "txns_last_5m",
    "txns_last_5m_sells",
    "txns_last_5m_buys",
    "rug_score",
    "twitter_followers",
    "discord_members",
}
_BOOL_INT_FIELDS = {"cluster_bad", "social_ok", "mint_auth_renounced", "insider_sig"}

_TREND_STR_TO_INT = {
    "up": 1,
    "uptrend": 1,
    "bull": 1,
    "bullish": 1,
    "down": -1,
    "downtrend": -1,
    "bear": -1,
    "bearish": -1,
    "flat": 0,
    "sideways": 0,
    "neutral": 0,
    "unknown": 0,
}
_PREF_KEYS = ("usd", "h24", "24h", "quote", "base", "value")

_DB_DEFAULTS: dict[str, Any] = {
    "liquidity_usd": 0.0,
    "volume_24h_usd": 0.0,
    "market_cap_usd": 0.0,
    "holders": 0,
    "rug_score": 0,
    "cluster_bad": 0,
    "social_ok": 0,
    "insider_sig": 0,
    "score_total": 0,
}


def _sanitize_address_inplace(token: dict) -> None:
    candidates: list[tuple[str, str, str]] = []

    for key in ("address", "token_address", "tokenAddress", "poolAddress"):
        val = token.get(key)
        if isinstance(val, str):
            candidates.append(("root", key, val))

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
            else:
                try:
                    token["baseToken"]["address"] = cleaned
                except Exception:
                    pass
            if picked is None:
                picked = cleaned

    if picked:
        if token.get("address") != picked:
            try:
                log.debug("[data] address normalizado %r -> %r", token.get("address"), picked)
            except Exception:
                pass
            token["address"] = picked
    else:
        addr = token.get("address")
        if isinstance(addr, str) and not is_probably_mint(addr):
            try:
                log.debug("[data] address no parece mint SPL: %r", addr)
            except Exception:
                pass


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
        return None
    if isinstance(value, dict):
        return _extract_from_dict(value, ctx)
    if isinstance(value, (list, tuple)) and value:
        return _to_float(value[0], ctx)
    try:
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except (ValueError, TypeError):
        log.debug("No convertible a float [%s] -> %s (%s)", ctx, value, type(value).__name__)
        return None


def is_missing_value(value: Any, *, treat_zero_as_missing: bool = False) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if treat_zero_as_missing:
        try:
            return float(value) == 0.0
        except Exception:
            return value == 0
    return False


def _normalize_trend(v: Any) -> int | None:
    if is_missing_value(v):
        return None
    if isinstance(v, (int, float)):
        return int(max(min(v, 1), -1))
    if isinstance(v, str):
        return _TREND_STR_TO_INT.get(v.lower().strip(), 0)
    return None


def _minutes_since(ts: dt.datetime | None) -> float | None:
    if not ts:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return (utc_now() - ts).total_seconds() / 60.0


def _coerce_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)
    if isinstance(value, str):
        parsed = parse_iso_utc(value)
        if parsed:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        return None
    try:
        epoch = float(value)
    except Exception:
        return None
    if epoch > 1e11:
        epoch /= 1000.0
    try:
        return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
    except Exception:
        return None


def _resolve_created_at(clean: Dict[str, Any]) -> dt.datetime | None:
    for key in (
        "created_at",
        "createdAt",
        "created",
        "createdAtUtc",
        "pairCreatedAt",
        "pair_created_at",
        "pairCreatedAtMs",
        "listedAt",
    ):
        parsed = _coerce_datetime(clean.get(key))
        if parsed:
            return parsed
    return None


def is_incomplete(tok: Dict[str, Any]) -> bool:
    liq = tok.get("liquidity_usd")
    vol = tok.get("volume_24h_usd")
    holders = tok.get("holders")
    return (
        is_missing_value(liq, treat_zero_as_missing=True)
        or is_missing_value(vol, treat_zero_as_missing=True)
        or is_missing_value(holders, treat_zero_as_missing=True)
    )


def fill_provisional_liq_vol(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().sort_values("timestamp")
    for col in ("liquidity_usd", "volume_24h_usd"):
        df[col] = df[col].ffill()
    return df


def sanitize_token_data(token: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normaliza el token sin destruir la información de ausencia real.
    """
    clean = token
    ctx = clean.get("symbol") or clean.get("address", "")[:4]

    try:
        _sanitize_address_inplace(clean)
    except Exception:
        pass

    for raw, canon in list(_NUMERIC_ALIASES.items()):
        if raw in clean:
            clean[canon] = _to_float(clean.pop(raw), ctx)

    for field in _FLOAT_FIELDS:
        if field in clean:
            clean[field] = _to_float(clean.get(field), ctx)

    for field in _INT_FIELDS:
        if field not in clean:
            continue
        num = _to_float(clean.get(field), ctx)
        clean[field] = None if is_missing_value(num) else int(num)

    for field in _BOOL_INT_FIELDS:
        if field in clean:
            clean[field] = None if is_missing_value(clean[field]) else int(bool(clean[field]))

    if "trend" in clean:
        clean["trend"] = _normalize_trend(clean["trend"])

    created_at = _resolve_created_at(clean)
    clean["created_at"] = created_at

    age_val = _minutes_since(created_at)
    if age_val is None:
        raw_age = _to_float(clean.get("age_minutes") or clean.get("age_min"), ctx)
        clean["age_minutes"] = clean["age_min"] = raw_age
    else:
        clean["age_minutes"] = clean["age_min"] = age_val

    clean.setdefault("fetched_at", utc_now())
    return clean


DEFAULTS = {
    "cluster_bad": 0,
    "mint_auth_renounced": 0,
    "insider_sig": 0,
    "score_total": 0,
}


def apply_default_values(tok: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in DEFAULTS.items():
        if is_missing_value(tok.get(key)):
            tok[key] = value
    return tok


def prepare_token_for_db(tok: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convierte el payload a una forma segura para persistencia SQL.
    No debe usarse para construir features T0.
    """
    out = dict(tok)
    for key, default in _DB_DEFAULTS.items():
        if is_missing_value(out.get(key)):
            out[key] = default

    for key in ("liquidity_usd", "volume_24h_usd", "market_cap_usd"):
        if key in out and not is_missing_value(out.get(key)):
            out[key] = float(out[key])

    for key in ("holders", "rug_score", "score_total"):
        if key in out and not is_missing_value(out.get(key)):
            out[key] = int(float(out[key]))

    for key in _BOOL_INT_FIELDS:
        if key in out and not is_missing_value(out.get(key)):
            out[key] = int(bool(out[key]))

    if "trend" in out:
        out["trend"] = None if is_missing_value(out.get("trend")) else str(out["trend"])

    return out


__all__ = [
    "sanitize_token_data",
    "is_incomplete",
    "is_missing_value",
    "fill_provisional_liq_vol",
    "apply_default_values",
    "prepare_token_for_db",
]
