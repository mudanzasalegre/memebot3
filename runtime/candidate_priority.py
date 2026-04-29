from __future__ import annotations

import datetime as dt
from typing import Any


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        out = float(value)
        if out != out:
            return float(default)
        return out
    except Exception:
        return float(default)


def _source_score(source: str) -> float:
    raw = str(source or "").strip().lower()
    if raw in {"pumpportal", "pump_portal"}:
        return 35.0
    if raw in {"pumpfun", "pump_fun", "pump"}:
        return 30.0
    return 0.0


def _age_minutes(token: dict[str, Any], now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now(dt.timezone.utc)
    created = token.get("created_at") or token.get("createdAt")
    if isinstance(created, str):
        try:
            created = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        except Exception:
            created = None
    if isinstance(created, dt.datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (now - created).total_seconds() / 60.0)
    for key in ("age_minutes", "age_min", "queue_age_minutes"):
        if token.get(key) is not None:
            return _to_float(token.get(key), 0.0)
    return 0.0


def candidate_priority_score(token: dict[str, Any], *, source: str | None = None, now: dt.datetime | None = None) -> float:
    src = source or token.get("source") or token.get("discovered_via")
    age_min = _age_minutes(token, now)
    price5m = _to_float(token.get("price_pct_5m"), 0.0)
    txns5m = _to_float(token.get("txns_last_5m"), 0.0)
    liq = _to_float(token.get("liquidity_usd"), 0.0)
    score = _source_score(str(src))
    score += max(0.0, 20.0 - age_min) * 1.5
    score += min(max(price5m, 0.0), 180.0) / 4.0
    score += min(txns5m, 180.0) / 4.0
    score += min(liq / 500.0, 15.0)
    return round(max(0.0, score), 3)


__all__ = ["candidate_priority_score"]
