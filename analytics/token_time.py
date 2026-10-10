from __future__ import annotations

import datetime as dt
import math
from copy import deepcopy
from numbers import Real
from typing import Any


# Venue creation/listing is a different grain, even for the same base mint.
BIRTH_CLOCK_FIELDS = _BIRTH_CLOCKS = ("created_at", "createdAt", "created", "createdAtUtc")
AGE_SEMANTICS_VERSION = "original_mint_birth_not_venue_v2"
_SEEN_CLOCKS = ("first_seen_epoch_s", "first_seen_at")


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


def parse_event_clock(value: Any, *, now: dt.datetime | None = None) -> dt.datetime | None:
    """Nullable typed UTC event time; its presence does not prove token birth."""
    parsed = _to_datetime(value)
    limit = _to_datetime(now) if now is not None else dt.datetime.now(dt.timezone.utc)
    return parsed if parsed is not None and limit is not None and parsed <= limit else None


def venue_clock_snapshot(token: dict[str, Any], *, created_at: Any, kind: str, source: str) -> dict[str, Any]:
    """Detach Pair/pool clocks without promoting raw aliases to mint birth.

    Keep bounded original clock metadata for diagnostics. This applies only to
    an adapter's venue/untyped metadata record, never to a queued known birth.
    """
    out = deepcopy(token)
    aliases = (*_BIRTH_CLOCKS, "createTime", "createUnixTime", "age_minutes", "age_min", "token_age_min",
               "listedAt", "listed_at", "launched_at", "launch_date")
    metadata = {key: out.pop(key) for key in aliases if key in out}
    event = parse_event_clock(created_at)
    out.update(created_at=None, age_minutes=None, age_min=None, token_age_min=None,
               pair_created_at=event, venue_clock_kind=kind,
               venue_clock_source=source, venue_clock_metadata=metadata)
    out["venue_clock"] = {"kind":kind, "source":source, "pair_address":out.get("pair_address"),
                          "created_at":event, "basis":"venue_event_not_mint_birth"}
    return out


def compute_age_minutes(token: dict[str, Any], now: dt.datetime | None = None) -> float | None:
    """Original birth or measured age, never queue residence/fabricated zero."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)

    created = None
    for key in _BIRTH_CLOCKS:
        created = _to_datetime(token.get(key))
        if created is not None:
            break
    if created is not None:
        return (now - created).total_seconds() / 60.0 if created <= now else None

    for key in ("age_minutes", "age_min", "token_age_min"):
        value = _to_float(token.get(key))
        if value is not None:
            return value
    return None


def compute_queue_age_minutes(token: dict[str, Any], now: dt.datetime | None = None) -> float | None:
    """Original queue first-seen or measured residence, never token birth age."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
    for key in _SEEN_CLOCKS:
        first_seen = _to_datetime(token.get(key))
        if first_seen is not None:
            return (now - first_seen).total_seconds() / 60.0 if first_seen <= now else None
    for key in ("queue_age_minutes", "minutes_since_first_seen"):
        value = _to_float(token.get(key))
        if value is not None:
            return value
    return None


def compute_shadow_age_minutes(token: dict[str, Any], now: dt.datetime | None = None) -> float | None:
    """Elapsed original observation time, never purchase time or token birth."""
    # A queue measurement and a shadow measurement are distinct observations.
    measured = {key: token.get(key) for key in _SEEN_CLOCKS}
    for key in ("minutes_since_first_seen", "shadow_age_min", "age_since_seen_min"):
        value = _to_float(token.get(key))
        if value is not None:
            measured["queue_age_minutes"] = value
            break
    return compute_queue_age_minutes(measured, now=now)


def compute_age_at_seen_minutes(token: dict[str, Any], now: dt.datetime | None = None) -> float | None:
    """Birth age at original observation, or a valid measured upper bound."""
    stamp = _to_datetime(now) if now is not None else dt.datetime.now(dt.timezone.utc)
    for key in _SEEN_CLOCKS:
        seen = _to_datetime(token.get(key))
        if seen is not None:
            if stamp is None or seen > stamp:
                return None
            # A measured current age is not a measurement at first-seen.
            if any(_to_datetime(token.get(k)) is not None for k in _BIRTH_CLOCKS):
                return compute_age_minutes(token, now=seen)
            break
    measured = _to_float(token.get("age_at_seen"))
    if measured is not None:
        return measured
    return compute_age_minutes(token, now=stamp)


def historical_age_snapshot(token: dict[str, Any]) -> dict[str, Any]:
    """Read-only view for labels/reports, evaluated at the recorded event clock.

    Birth, discovery, purchase, update and close clocks cannot stand in for the
    event clock. If the latter is missing, timestamp-derived ages stay unknown;
    genuine measured ages are still usable. Originals are never rewritten.
    This freezes temporal features, not outcome availability or execution proof.
    """
    stamp = next((parsed for key in ("decision_at", "ts_utc", "timestamp")
                  if (parsed := _to_datetime(token.get(key))) is not None), None)
    birth_known = any(_to_datetime(token.get(k)) is not None for k in _BIRTH_CLOCKS)
    seen_known = any(_to_datetime(token.get(k)) is not None for k in _SEEN_CLOCKS)
    view = {k: v for k, v in token.items() if k not in (*_BIRTH_CLOCKS, *_SEEN_CLOCKS)}
    age = None if stamp is None and birth_known else compute_age_minutes(view if stamp is None else token, now=stamp)
    queue_age = None if stamp is None and seen_known else compute_queue_age_minutes(view if stamp is None else token, now=stamp)
    shadow_age = None if stamp is None and seen_known else compute_shadow_age_minutes(view if stamp is None else token, now=stamp)
    age_at_seen = (_to_float(token.get("age_at_seen")) if stamp is None and (birth_known or seen_known)
                   else compute_age_at_seen_minutes(view if stamp is None else token, now=stamp))
    for key in ("age_min", "token_age_min", "minutes_since_first_seen", "shadow_age_min", "age_since_seen_min"):
        view.pop(key, None)
    view.update(age_minutes=age, queue_age_minutes=queue_age, shadow_age_min=shadow_age, age_at_seen=age_at_seen)
    return view


def token_with_age(token: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    out = dict(token)
    out["age_minutes"] = compute_age_minutes(out, now=now)
    return out


__all__ = ["compute_age_minutes", "compute_queue_age_minutes", "compute_shadow_age_minutes",
           "compute_age_at_seen_minutes", "historical_age_snapshot", "token_with_age",
           "BIRTH_CLOCK_FIELDS", "AGE_SEMANTICS_VERSION", "parse_event_clock", "venue_clock_snapshot"]
