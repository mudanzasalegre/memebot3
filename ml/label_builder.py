from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _num(frame: pd.DataFrame, *columns: str, default: float = np.nan) -> pd.Series:
    for column in columns:
        if column in frame.columns:
            return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(default, index=frame.index, dtype="float64")


def _clip(series: pd.Series, low: float = -100.0, high: float = 500.0) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").clip(float(low), float(high))


def build_labels(frame: pd.DataFrame, *, capture_factor: float = 0.35) -> pd.DataFrame:
    realized = _num(frame, "realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct")
    peak = _num(frame, "max_pnl_seen", "max_pnl_pct_seen", "peak_pnl_pct", "max_pnl_pct").fillna(realized)
    seen_1m = _num(frame, "max_pnl_after_seen_1m", "continuation_peak_after_seen_1m").fillna(np.nan)
    seen_3m = _num(frame, "max_pnl_after_seen_3m", "continuation_peak_after_seen_3m").fillna(np.nan)
    out = pd.DataFrame(index=frame.index)
    out["is_winner"] = realized.gt(0).fillna(False).astype(int)
    out["severe_loss_30"] = realized.le(-30).fillna(False).astype(int)
    out["severe_loss_50"] = realized.le(-50).fillna(False).astype(int)
    for threshold in (50, 100, 300, 500):
        out[f"runner_{threshold}"] = peak.ge(float(threshold)).fillna(False).astype(int)
    out["continuation_1m"] = seen_1m
    out["continuation_3m"] = seen_3m
    out["continuation_peak_after_seen_1m"] = seen_1m
    out["continuation_peak_after_seen_3m"] = seen_3m
    out["continuation_drawdown_after_seen"] = _num(frame, "continuation_drawdown_after_seen", "drawdown_after_seen")
    out["continuation_positive_after_seen"] = seen_3m.gt(0).fillna(False).astype(int)
    out["ev_realized"] = _clip(realized)
    out["ev_realized_clipped"] = out["ev_realized"]
    out["ev_peak_adjusted"] = _clip(peak.fillna(0.0) * float(capture_factor))
    out["capture_ratio"] = realized / peak.where(peak > 0)
    out["capture_ratio"] = out["capture_ratio"].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return out


def attach_labels(frame: pd.DataFrame, *, capture_factor: float = 0.35) -> pd.DataFrame:
    labels = build_labels(frame, capture_factor=capture_factor)
    out = frame.copy()
    for column in labels.columns:
        out[column] = labels[column]
    return out


LABEL_DOCUMENTATION: dict[str, str] = {
    "is_winner": "realized return is positive",
    "severe_loss_30": "realized return is <= -30%",
    "severe_loss_50": "realized return is <= -50%",
    "runner_50/100/300/500": "post-entry peak return reached the threshold",
    "continuation_1m/3m": "post-seen peak continuation over the horizon",
    "ev_realized": "clipped realized return",
    "ev_peak_adjusted": "clipped peak return times capture factor",
    "capture_ratio": "realized return divided by peak return when peak is positive",
}


__all__ = ["LABEL_DOCUMENTATION", "attach_labels", "build_labels"]
