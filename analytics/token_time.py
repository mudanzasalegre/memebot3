from __future__ import annotations

import datetime as dt
import math
from numbers import Real
from typing import Any


def _to_datetime(value: Any) -> dt.datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, Real):
        try:
            raw = float(value)
            if not math.isfinite(raw) or raw <= 0:
                return None
            if raw >= 1e17:
                raw /= 1e9
            elif raw >= 1e14:
                raw /= 1e6
            elif raw >= 1e11:
                raw /= 1e3
            parsed = dt.datetime.fromtimestamp(raw, tz=dt.timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            if raw.isdigit():
                return _to_datetime(float(raw))
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _to_float(value: Any) -> float | None:
    try:
        if isinstance(value, bool) or not isinstance(value, (str, Real)):
            return None
        if isinstance(value, str) and not value.strip():
            return None
        out = float(value)
        if not math.isfinite(out) or out < 0:
            return None
        return out
    except Exception:
        return None


def compute_age_minutes(token: dict[str, Any], now: dt.datetime | None = None) -> float | None:
    """Original birth or measured age, never queue residence/fabricated zero."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)

    created = None
    for key in ("created_at", "createdAt", "created", "createdAtUtc", "pairCreatedAt", "pair_created_at", "pairCreatedAtMs"):
        created = _to_datetime(token.get(key))
        if created is not None:
            break
    if created is not None:
        return (now - created).total_seconds() / 60.0 if created <= now else None

    for key in ("age_minutes", "age_min"):
        value = _to_float(token.get(key))
        if value is not None:
            return value
    return None


def token_with_age(token: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    out = dict(token)
    out["age_minutes"] = compute_age_minutes(out, now=now)
    return out


__all__ = ["compute_age_minutes", "token_with_age"]
