from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from config.config import PROJECT_ROOT

EVENTS_PATH = PROJECT_ROOT / "data" / "metrics" / "runtime_events.jsonl"


def _tail_text_lines(path: Path, *, limit: int) -> list[str]:
    if limit <= 0 or not path.exists() or not path.is_file():
        return []
    try:
        size = path.stat().st_size
    except Exception:
        return []
    if size <= 0:
        return []

    data = bytearray()
    line_breaks = 0
    chunk_size = 8192
    try:
        with path.open("rb") as handle:
            offset = size
            while offset > 0 and line_breaks <= limit:
                read_size = min(chunk_size, offset)
                offset -= read_size
                handle.seek(offset)
                chunk = handle.read(read_size)
                data[:0] = chunk
                line_breaks += chunk.count(b"\n")
    except Exception:
        return []
    return bytes(data).decode("utf-8", errors="ignore").splitlines()[-int(limit) :]


def _read_jsonl_tail(path: Path, *, max_rows: int | None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    if max_rows is None:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            lines = list(handle)
    else:
        lines = _tail_text_lines(path, limit=max(1, int(max_rows)))
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except Exception:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _load_events(path: Path = EVENTS_PATH, *, max_rows: int | None = 5_000) -> pd.DataFrame:
    return pd.DataFrame(_read_jsonl_tail(path, max_rows=max_rows))


def drift_snapshot(*, window: int = 50, events_path: Path = EVENTS_PATH, max_rows: int | None = 5_000) -> dict[str, Any]:
    df = _load_events(events_path, max_rows=max_rows)
    if df.empty:
        return {"rows": 0, "degraded": False, "reason": "no_events"}
    closed = df[df.get("event_type", pd.Series("", index=df.index)).astype("string").isin(["candidate_outcome", "trade_close", "shadow_close"])].tail(int(window))
    if closed.empty:
        pnl = pd.Series(dtype="float64")
    elif "pnl_pct" in closed.columns:
        pnl = pd.to_numeric(closed["pnl_pct"], errors="coerce")
    elif "target_total_pnl_pct" in closed.columns:
        pnl = pd.to_numeric(closed["target_total_pnl_pct"], errors="coerce")
    else:
        pnl = pd.Series(dtype="float64")
    severe = pnl.le(-30.0)
    missed = df[df.get("event_type", pd.Series("", index=df.index)).astype("string").eq("ml_policy_decision")]
    if not missed.empty and "target_total_pnl_pct" in missed.columns:
        missed_pnl = pd.to_numeric(missed["target_total_pnl_pct"], errors="coerce")
        missed_jackpots = int(missed_pnl.ge(100.0).sum())
    else:
        missed_jackpots = 0
    degraded = bool(missed_jackpots >= 2 or (len(pnl.dropna()) > 0 and severe.mean() > 0.25))
    return {
        "rows": int(len(closed)),
        "win_rate": float(pnl.gt(0).mean()) if len(pnl.dropna()) else None,
        "avg_pnl": float(pnl.mean()) if len(pnl.dropna()) else None,
        "severe_loss_rate": float(severe.mean()) if len(pnl.dropna()) else None,
        "missed_jackpots": missed_jackpots,
        "sampled_rows": int(len(df)) if max_rows is not None else None,
        "degraded": degraded,
        "reason": "degradation" if degraded else "ok",
    }


def effective_mode(base_mode: str, *, lane: str | None = None, snapshot: dict[str, Any] | None = None) -> str:
    snap = snapshot or drift_snapshot()
    if snap.get("degraded") and str(base_mode).lower() == "enforce":
        return "shadow"
    return str(base_mode)


__all__ = ["drift_snapshot", "effective_mode"]
