from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from analytics.report_utils import metrics_dir, write_json
from config.config import CFG, PROJECT_ROOT


_STATE: dict[str, Any] = {"active": {}, "events": []}


def _cfg_bool(cfg: Any, name: str, default: bool) -> bool:
    value = getattr(cfg, name, default)
    if isinstance(value, bool):
        return value
    raw = str(value if value is not None else default).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _cfg_float(cfg: Any, name: str, default: float) -> float:
    try:
        return float(getattr(cfg, name, default) or default)
    except Exception:
        return float(default)


def enabled(*, dry_run: bool, cfg: Any = CFG) -> bool:
    if not _cfg_bool(cfg, "RUNNER_TURBO_MONITOR_ENABLED", True):
        return False
    if _cfg_bool(cfg, "RUNNER_TURBO_PAPER_ONLY", True) and not bool(dry_run):
        return False
    return True


def _now(now: dt.datetime | None = None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)


def _event(event: str, address: str, *, now: dt.datetime, **extra: Any) -> None:
    events = _STATE.setdefault("events", [])
    if not isinstance(events, list):
        events = []
        _STATE["events"] = events
    events.append({"event": event, "address": address, "ts_utc": now.isoformat(), **extra})
    del events[:-200]


def observe_position(
    address: str,
    *,
    peak_pct: float,
    closed: bool = False,
    dry_run: bool = True,
    now: dt.datetime | None = None,
    cfg: Any = CFG,
) -> dict[str, Any]:
    ts = _now(now)
    key = str(address or "")
    if not key or not enabled(dry_run=dry_run, cfg=cfg):
        return {"active": False, "reason": "disabled"}
    active = _STATE.setdefault("active", {})
    if not isinstance(active, dict):
        active = {}
        _STATE["active"] = active
    if closed:
        return mark_closed(key, now=ts)

    existing = active.get(key)
    if isinstance(existing, dict):
        expires_at = dt.datetime.fromisoformat(str(existing.get("expires_at_utc")))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
        if ts >= expires_at:
            active.pop(key, None)
            _event("turbo_exit", key, now=ts, reason="expired")
            write_runner_turbo_monitor_report()
            return {"active": False, "reason": "expired"}
        return {"active": True, "reason": "already_active", **existing}

    threshold = _cfg_float(cfg, "RUNNER_TURBO_PEAK_PCT", 100.0)
    if float(peak_pct or 0.0) < threshold:
        return {"active": False, "reason": "below_threshold"}

    duration_min = max(0.1, _cfg_float(cfg, "RUNNER_TURBO_MAX_DURATION_MIN", 20.0))
    state = {
        "address": key,
        "entered_at_utc": ts.isoformat(),
        "expires_at_utc": (ts + dt.timedelta(minutes=duration_min)).isoformat(),
        "peak_pct_at_entry": float(peak_pct),
        "target_interval_s": max(0.1, _cfg_float(cfg, "RUNNER_TURBO_INTERVAL_S", 1.0)),
        "best_effort": True,
        "paper_only": _cfg_bool(cfg, "RUNNER_TURBO_PAPER_ONLY", True),
    }
    active[key] = state
    _event("turbo_enter", key, now=ts, peak_pct=float(peak_pct))
    write_runner_turbo_monitor_report()
    return {"active": True, "reason": "entered", **state}


def mark_closed(address: str, *, now: dt.datetime | None = None) -> dict[str, Any]:
    ts = _now(now)
    key = str(address or "")
    active = _STATE.setdefault("active", {})
    if isinstance(active, dict) and key in active:
        active.pop(key, None)
        _event("turbo_exit", key, now=ts, reason="closed")
        write_runner_turbo_monitor_report()
        return {"active": False, "reason": "closed"}
    return {"active": False, "reason": "not_active"}


def target_sleep_seconds(default_sleep_s: float, *, dry_run: bool = True, cfg: Any = CFG) -> float:
    if not enabled(dry_run=dry_run, cfg=cfg):
        return float(default_sleep_s)
    active = _STATE.get("active")
    if not isinstance(active, dict) or not active:
        return float(default_sleep_s)
    interval = max(0.1, _cfg_float(cfg, "RUNNER_TURBO_INTERVAL_S", 1.0))
    return min(float(default_sleep_s), interval)


def build_runner_turbo_monitor_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    active = _STATE.get("active") if isinstance(_STATE.get("active"), dict) else {}
    events = _STATE.get("events") if isinstance(_STATE.get("events"), list) else []
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "enabled": bool(getattr(CFG, "RUNNER_TURBO_MONITOR_ENABLED", True)),
            "peak_pct": float(getattr(CFG, "RUNNER_TURBO_PEAK_PCT", 100.0) or 100.0),
            "interval_s": float(getattr(CFG, "RUNNER_TURBO_INTERVAL_S", 1.0) or 1.0),
            "max_duration_min": float(getattr(CFG, "RUNNER_TURBO_MAX_DURATION_MIN", 20.0) or 20.0),
            "paper_only": bool(getattr(CFG, "RUNNER_TURBO_PAPER_ONLY", True)),
        },
        "active_count": len(active or {}),
        "active": active,
        "events": events[-100:],
        "best_effort_note": "The main loop uses the turbo interval as a target sleep while active; provider latency can make real polling slower.",
    }


def write_runner_turbo_monitor_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_runner_turbo_monitor_report(root)
    write_json(metrics_dir(root) / "runner_turbo_monitor_report.json", report)
    return report


def reset_state() -> None:
    _STATE["active"] = {}
    _STATE["events"] = []


__all__ = [
    "build_runner_turbo_monitor_report",
    "enabled",
    "mark_closed",
    "observe_position",
    "reset_state",
    "target_sleep_seconds",
    "write_runner_turbo_monitor_report",
]
