from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from config.config import PROJECT_ROOT

EVENTS_PATH = PROJECT_ROOT / "data" / "metrics" / "runtime_events.jsonl"


def _load_events(path: Path = EVENTS_PATH) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    rows = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return pd.DataFrame(rows)


def drift_snapshot(*, window: int = 50, events_path: Path = EVENTS_PATH) -> dict[str, Any]:
    df = _load_events(events_path)
    if df.empty:
        return {"rows": 0, "degraded": False, "reason": "no_events"}
    closed = df[df.get("event_type", pd.Series("", index=df.index)).astype("string").isin(["candidate_outcome", "trade_close", "shadow_close"])].tail(int(window))
    pnl = pd.to_numeric(closed.get("pnl_pct", closed.get("target_total_pnl_pct")), errors="coerce")
    severe = pnl.le(-30.0)
    missed = df[df.get("event_type", pd.Series("", index=df.index)).astype("string").eq("ml_policy_decision")]
    missed_jackpots = int(pd.to_numeric(missed.get("target_total_pnl_pct"), errors="coerce").ge(100.0).sum()) if not missed.empty else 0
    degraded = bool(missed_jackpots >= 2 or (len(pnl.dropna()) > 0 and severe.mean() > 0.25))
    return {
        "rows": int(len(closed)),
        "win_rate": float(pnl.gt(0).mean()) if len(pnl.dropna()) else None,
        "avg_pnl": float(pnl.mean()) if len(pnl.dropna()) else None,
        "severe_loss_rate": float(severe.mean()) if len(pnl.dropna()) else None,
        "missed_jackpots": missed_jackpots,
        "degraded": degraded,
        "reason": "degradation" if degraded else "ok",
    }


def effective_mode(base_mode: str, *, lane: str | None = None, snapshot: dict[str, Any] | None = None) -> str:
    snap = snapshot or drift_snapshot()
    if snap.get("degraded") and str(base_mode).lower() == "enforce":
        return "shadow"
    return str(base_mode)


__all__ = ["drift_snapshot", "effective_mode"]
