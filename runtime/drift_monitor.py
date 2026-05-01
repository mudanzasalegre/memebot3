from __future__ import annotations

from pathlib import Path
from typing import Any

from analytics.drift_monitor import drift_snapshot


def degrade_policy_mode(mode: str, *, lane: str | None = None, snapshot: dict[str, Any] | None = None) -> str:
    _ = lane
    snap = snapshot or drift_snapshot()
    raw = str(mode or "observe").strip().lower()
    if not snap.get("degraded"):
        return raw
    if raw == "enforce":
        return "shadow"
    if raw == "canary":
        return "shadow"
    if raw == "sizing_only":
        return "sizing_only"
    return raw


def drift_snapshot_for_runtime(*, window: int = 50, events_path: Path | None = None) -> dict[str, Any]:
    kwargs = {"window": window}
    if events_path is not None:
        kwargs["events_path"] = events_path
    return drift_snapshot(**kwargs)


__all__ = ["degrade_policy_mode", "drift_snapshot_for_runtime"]
