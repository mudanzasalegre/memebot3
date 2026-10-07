"""Client-receipt provenance, not a claim about a provider's market as-of time."""
from __future__ import annotations

from copy import deepcopy
import math
import time
from typing import Any

OBSERVATION_VERSION = "provider_http_receipt_v1"
DEFAULT_MAX_AGE_S = 30.0
MARKET_FIELDS = (
    "price_usd", "price_native", "liquidity_usd", "market_cap_usd",
    "volume_24h_usd", "txns_last_5m", "txns_last_5m_buys",
    "txns_last_5m_sells", "holders", "price_pct_1m", "price_pct_5m",
    "volume_pct_5m",
)
_SIGNED_FIELDS = {"price_pct_1m", "price_pct_5m", "volume_pct_5m"}


def market_number(value: Any, field: str) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if field in {"price_usd", "price_native"}:
        return number if number > 0 else None
    return number if field in _SIGNED_FIELDS or number >= 0 else None


def stamp_market_observation(
    payload: dict, source: str, *, received_at: float | None = None,
) -> dict:
    """Call only after receiving HTTP data; cache reads must retain this stamp."""
    out = deepcopy(payload)
    received = time.time() if received_at is None else received_at
    out.pop("market_observation", None)  # Never trust provider-supplied local proof.
    fields = {}
    for field in MARKET_FIELDS:
        value = market_number(out.get(field), field)
        if value is not None:
            fields[field] = {"source": source, "received_at": received, "value": value}
    out["market_observation"] = {
        "version": OBSERVATION_VERSION,
        "address": out.get("address"),
        "basis": "http_response_received_not_provider_market_asof",
        "fields": fields,
    }
    return out


def fresh_market_value(
    payload: dict | None, field: str, *, max_age_s: float = DEFAULT_MAX_AGE_S,
    now: float | None = None, source: str | None = None,
) -> float | None:
    """Unknown, mismatched, nonfinite, stale or future-dated data is not fresh."""
    if not isinstance(payload, dict):
        return None
    value = market_number(payload.get(field), field)
    proof = payload.get("market_observation")
    if value is None or not isinstance(proof, dict):
        return None
    if (proof.get("version") != OBSERVATION_VERSION
            or proof.get("basis") != "http_response_received_not_provider_market_asof"
            or not payload.get("address") or proof.get("address") != payload.get("address")):
        return None
    fields = proof.get("fields")
    record = fields.get(field) if isinstance(fields, dict) else None
    if not isinstance(record, dict) or not record.get("source"):
        return None
    if source is not None and record.get("source") != source:
        return None
    if market_number(record.get("value"), field) != value:
        return None
    received = record.get("received_at")
    if isinstance(received, bool):
        return None
    try:
        received = float(received)
        age = (time.time() if now is None else float(now)) - received
        limit = float(max_age_s)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(x) for x in (received, age, limit)) or received <= 0:
        return None
    return value if limit > 0 and -2.0 <= age <= limit else None


def retain_fresh_market_fields(payload: dict | None, *, max_age_s: float = DEFAULT_MAX_AGE_S) -> dict | None:
    if not isinstance(payload, dict):
        return None
    out = deepcopy(payload)
    for field in MARKET_FIELDS:
        out[field] = fresh_market_value(payload, field, max_age_s=max_age_s)
    # Remove aliases too, so coercion cannot resurrect rejected raw values.
    for key in ("priceUsd", "priceNative", "liquidity", "volume", "fdv", "mcap"):
        out.pop(key, None)
    return out


def liquidity_crushed(buy_liquidity: Any, current_liquidity: Any, fraction: Any) -> bool:
    """A measured zero is a collapse; missing/invalid liquidity is unknown."""
    bought = market_number(buy_liquidity, "liquidity_usd")
    current = market_number(current_liquidity, "liquidity_usd")
    ratio = market_number(fraction, "fraction")
    return (bought is not None and bought > 0 and current is not None
            and ratio is not None and 0 < ratio <= 1 and current <= bought * ratio)
