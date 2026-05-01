from __future__ import annotations

import datetime as dt
from typing import Any


def _to_datetime(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        raw = float(value)
        if raw <= 0:
            return None
        if raw > 10_000_000_000:
            raw /= 1000.0
        parsed = dt.datetime.fromtimestamp(raw, tz=dt.timezone.utc)
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
        if value is None or value == "":
            return None
        out = float(value)
        if out != out:
            return None
        return out
    except Exception:
        return None


def compute_age_minutes(token: dict[str, Any], now: dt.datetime | None = None) -> float:
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
        return max(0.0, (now - created).total_seconds() / 60.0)

    for key in ("age_minutes", "age_min", "queue_age_minutes"):
        value = _to_float(token.get(key))
        if value is not None:
            return max(0.0, value)
    return 0.0


def token_with_age(token: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    out = dict(token)
    out["age_minutes"] = compute_age_minutes(out, now=now)
    return out


__all__ = ["compute_age_minutes", "token_with_age"]
