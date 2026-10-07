from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ml.labels import build_moonshot_labels


MOONSHOT_BINARY_LABELS = (
    "theoretical_moonshot",
    "executable_moonshot",
    "moonshot_paper_only",
    "moonshot_live_enabled",
)
MOONSHOT_NUMERIC_LABELS = (
    "moonshot_peak_pct",
    "moonshot_time_to_peak_min",
    "moonshot_amount_sol",
)

RUNNER_THRESHOLDS = (50, 100, 200, 300, 500, 1000, 2000, 5000, 10000)


def _num(frame: pd.DataFrame, *columns: str, default: float = np.nan) -> pd.Series:
    out = pd.Series(default, index=frame.index, dtype="float64")
    for column in columns:
        if column in frame.columns:
            values = pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
            out = out.fillna(values)
    return out


def _clip(series: pd.Series, low: float = -100.0, high: float = 500.0) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").clip(float(low), float(high))


def _text_any(frame: pd.DataFrame, *columns: str) -> pd.Series:
    out = pd.Series("", index=frame.index, dtype="string")
    for column in columns:
        if column in frame.columns:
            out = out.str.cat(frame[column].fillna("").astype("string"), sep=" ")
    return out.str.upper()


def build_labels(frame: pd.DataFrame, *, capture_factor: float = 0.35) -> pd.DataFrame:
    realized = _num(frame, "realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct")
    peak = _num(frame, "max_pnl_seen", "max_pnl_pct_seen", "peak_pnl_pct", "max_pnl_pct")
    seen_1m = _num(frame, "max_pnl_after_seen_1m", "continuation_peak_after_seen_1m").fillna(np.nan)
    seen_3m = _num(frame, "max_pnl_after_seen_3m", "continuation_peak_after_seen_3m").fillna(np.nan)
    out = pd.DataFrame(index=frame.index)
    out["is_winner"] = realized.gt(0).astype("Int64").where(realized.notna())
    out["severe_loss_30"] = realized.le(-30).astype("Int64").where(realized.notna())
    out["severe_loss_50"] = realized.le(-50).astype("Int64").where(realized.notna())
    exit_text = _text_any(frame, "exit_reason", "exit_reason_full", "reason", "green_sniper_reason", "reject_reason")
    reason_known = exit_text.str.strip().ne("")
    loss_observed = realized.notna() & reason_known
    out["liquidity_crush_loss"] = (exit_text.str.contains("LIQUIDITY_CRUSH", regex=False) & realized.lt(0)).astype("Int64").where(loss_observed)
    out["toxic_exit_loss"] = (
        exit_text.str.contains("LIQUIDITY_CRUSH", regex=False)
        | exit_text.str.contains("NO_PUMP_EXIT", regex=False)
        | exit_text.str.contains("ADVERSE_TICK", regex=False)
    ).fillna(False).astype("Int64").where(loss_observed)
    for threshold in RUNNER_THRESHOLDS:
        out[f"runner_{threshold}"] = peak.ge(float(threshold)).astype("Int64").where(peak.notna())
    out["continuation_1m"] = seen_1m
    out["continuation_3m"] = seen_3m
    out["continuation_peak_after_seen_1m"] = seen_1m
    out["continuation_peak_after_seen_3m"] = seen_3m
    out["continuation_drawdown_after_seen"] = _num(frame, "continuation_drawdown_after_seen", "drawdown_after_seen")
    out["continuation_positive_after_seen"] = seen_3m.gt(0).astype("Int64").where(seen_3m.notna())
    out["ev_realized"] = _clip(realized)
    out["ev_realized_clipped"] = out["ev_realized"]
    out["ev_peak_adjusted"] = _clip(peak * float(capture_factor))
    out["capture_ratio"] = realized / peak.where(peak > 0)
    out["capture_ratio"] = out["capture_ratio"].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    moonshot = build_moonshot_labels(frame)
    for column in MOONSHOT_BINARY_LABELS:
        if column in moonshot.columns:
            out[column] = moonshot[column].fillna(False).astype(int)
    for column in MOONSHOT_NUMERIC_LABELS:
        if column in moonshot.columns:
            out[column] = pd.to_numeric(moonshot[column], errors="coerce")
    moonshot_peak = pd.to_numeric(out["moonshot_peak_pct"], errors="coerce").fillna(0.0) if "moonshot_peak_pct" in out.columns else pd.Series(0.0, index=out.index)
    executable = (
        pd.to_numeric(out["executable_moonshot"], errors="coerce").fillna(0).astype(int).astype(bool)
        if "executable_moonshot" in out.columns
        else pd.Series(False, index=out.index)
    )
    theoretical = (
        pd.to_numeric(out["theoretical_moonshot"], errors="coerce").fillna(0).astype(int).astype(bool)
        if "theoretical_moonshot" in out.columns
        else pd.Series(False, index=out.index)
    )
    for threshold in (100, 500):
        out[f"moonshot_peak{threshold}"] = (theoretical & moonshot_peak.ge(float(threshold))).astype(int)
        out[f"executable_moonshot_peak{threshold}"] = (executable & moonshot_peak.ge(float(threshold))).astype(int)
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
    "liquidity_crush_loss": "loss closed or labeled as LIQUIDITY_CRUSH",
    "toxic_exit_loss": "row closed or labeled as LIQUIDITY_CRUSH, NO_PUMP_EXIT, or ADVERSE_TICK",
    "runner_50/100/200/300/500/1000/2000/5000/10000": "observed post-entry peak return reached the threshold; unknown peaks remain unlabelled, not negative",
    "continuation_1m/3m": "post-seen peak continuation over the horizon",
    "ev_realized": "clipped realized return",
    "ev_peak_adjusted": "clipped peak return times capture factor",
    "capture_ratio": "realized return divided by peak return when peak is positive",
    "executable_moonshot": "moonshot micro row passes paper executable viability checks",
    "theoretical_moonshot": "row has moonshot markers or later moonshot-size peak",
    "executable_moonshot_peak100/500": "executable moonshot row whose later peak reached the threshold",
}


__all__ = ["LABEL_DOCUMENTATION", "RUNNER_THRESHOLDS", "attach_labels", "build_labels"]
