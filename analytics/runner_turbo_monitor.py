from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from analytics.report_utils import filter_test_events, metrics_dir, read_jsonl, write_json
from config.config import CFG, PROJECT_ROOT
from utils.runtime_context import runtime_context_payload


EVENTS_FILE = "runner_turbo_events.jsonl"
EVENT_RUNNER_TURBO_ENTER = "RUNNER_TURBO_ENTER"
EVENT_RUNNER_TURBO_TICK = "RUNNER_TURBO_TICK"
EVENT_RUNNER_TURBO_EXIT = "RUNNER_TURBO_EXIT"
EVENT_RUNNER_TURBO_CLOSE_TRIGGERED = "RUNNER_TURBO_CLOSE_TRIGGERED"
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


def _event_path(root: Path | None = None) -> Path:
    return metrics_dir(root or PROJECT_ROOT) / EVENTS_FILE


def _json_safe(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _persist_event(row: dict[str, Any], *, root: Path | None = None) -> None:
    path = _event_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_json_safe(row), ensure_ascii=True, sort_keys=True) + "\n")


def _event(
    event: str,
    address: str,
    *,
    now: dt.datetime,
    persist: bool = True,
    run_id: str | None = None,
    test_event: bool | None = None,
    **extra: Any,
) -> None:
    row = {"event": event, "address": address, "ts_utc": now.isoformat()}
    row.update(runtime_context_payload(run_id=run_id, test_event=test_event))
    row.update(extra)
    events = _STATE.setdefault("events", [])
    if not isinstance(events, list):
        events = []
        _STATE["events"] = events
    events.append(row)
    del events[:-200]
    if persist:
        try:
            _persist_event(row)
        except Exception:
            return


def observe_position(
    address: str,
    *,
    peak_pct: float,
    closed: bool = False,
    dry_run: bool = True,
    now: dt.datetime | None = None,
    cfg: Any = CFG,
    run_id: str | None = None,
    test_event: bool | None = None,
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
        return mark_closed(key, now=ts, run_id=run_id, test_event=test_event)

    existing = active.get(key)
    if isinstance(existing, dict):
        expires_at = dt.datetime.fromisoformat(str(existing.get("expires_at_utc")))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
        if ts >= expires_at:
            active.pop(key, None)
            _event(
                EVENT_RUNNER_TURBO_EXIT,
                key,
                now=ts,
                reason="expired",
                peak_pct=float(peak_pct or 0.0),
                dry_run=bool(dry_run),
                run_id=run_id,
                test_event=test_event,
            )
            write_runner_turbo_monitor_report()
            return {"active": False, "reason": "expired"}
        _event(
            EVENT_RUNNER_TURBO_TICK,
            key,
            now=ts,
            peak_pct=float(peak_pct or 0.0),
            dry_run=bool(dry_run),
            run_id=run_id,
            test_event=test_event,
        )
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
    _event(
        EVENT_RUNNER_TURBO_ENTER,
        key,
        now=ts,
        peak_pct=float(peak_pct),
        dry_run=bool(dry_run),
        paper_only=_cfg_bool(cfg, "RUNNER_TURBO_PAPER_ONLY", True),
        run_id=run_id,
        test_event=test_event,
    )
    write_runner_turbo_monitor_report()
    return {"active": True, "reason": "entered", **state}


def record_close_triggered(
    address: str,
    *,
    reason: str,
    peak_pct: float | None = None,
    pnl_pct: float | None = None,
    dry_run: bool = True,
    now: dt.datetime | None = None,
    run_id: str | None = None,
    test_event: bool | None = None,
) -> dict[str, Any]:
    ts = _now(now)
    key = str(address or "")
    active = _STATE.get("active")
    if not key or not isinstance(active, dict) or key not in active:
        return {"active": False, "reason": "not_active"}
    _event(
        EVENT_RUNNER_TURBO_CLOSE_TRIGGERED,
        key,
        now=ts,
        reason=str(reason or ""),
        peak_pct=float(peak_pct or 0.0),
        pnl_pct=float(pnl_pct or 0.0),
        dry_run=bool(dry_run),
        run_id=run_id,
        test_event=test_event,
    )
    write_runner_turbo_monitor_report()
    return {"active": True, "reason": "close_triggered"}


def mark_closed(
    address: str,
    *,
    now: dt.datetime | None = None,
    run_id: str | None = None,
    test_event: bool | None = None,
) -> dict[str, Any]:
    ts = _now(now)
    key = str(address or "")
    active = _STATE.setdefault("active", {})
    if isinstance(active, dict) and key in active:
        active.pop(key, None)
        _event(EVENT_RUNNER_TURBO_EXIT, key, now=ts, reason="closed", run_id=run_id, test_event=test_event)
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


def build_runner_turbo_monitor_report(root: Path | None = None, *, include_test_events: bool | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    active = _STATE.get("active") if isinstance(_STATE.get("active"), dict) else {}
    persisted_events = read_jsonl(_event_path(root))
    memory_events = _STATE.get("events") if isinstance(_STATE.get("events"), list) else []
    merged_events = filter_test_events([*persisted_events, *memory_events], include_test_events=include_test_events)
    if not include_test_events and root == PROJECT_ROOT:
        merged_events = [
            event
            for event in merged_events
            if not (
                str(event.get("address") or "").strip().upper() in {"A", "SMOKE"}
                and not str(event.get("run_id") or "").strip()
            )
        ]
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for event in merged_events:
        key = (
            str(event.get("event") or ""),
            str(event.get("address") or ""),
            str(event.get("ts_utc") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    event_counts: dict[str, int] = {}
    for event in deduped:
        name = str(event.get("event") or "unknown")
        event_counts[name] = event_counts.get(name, 0) + 1
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
        "events_path": str(_event_path(root)),
        "include_test_events": bool(include_test_events),
        "event_counts": dict(sorted(event_counts.items())),
        "events": deduped[-100:],
        "best_effort_note": "The main loop uses the turbo interval as a target sleep while active; provider latency can make real polling slower.",
    }


def write_runner_turbo_monitor_report(root: Path | None = None, *, include_test_events: bool | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_runner_turbo_monitor_report(root, include_test_events=include_test_events)
    write_json(metrics_dir(root) / "runner_turbo_monitor_report.json", report)
    return report


def reset_state(*, clear_persisted: bool = False) -> None:
    _STATE["active"] = {}
    _STATE["events"] = []
    if clear_persisted:
        try:
            _event_path().unlink(missing_ok=True)
        except Exception:
            pass


__all__ = [
    "EVENT_RUNNER_TURBO_CLOSE_TRIGGERED",
    "EVENT_RUNNER_TURBO_ENTER",
    "EVENT_RUNNER_TURBO_EXIT",
    "EVENT_RUNNER_TURBO_TICK",
    "EVENTS_FILE",
    "build_runner_turbo_monitor_report",
    "enabled",
    "mark_closed",
    "observe_position",
    "record_close_triggered",
    "reset_state",
    "target_sleep_seconds",
    "write_runner_turbo_monitor_report",
]
