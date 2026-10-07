from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def severe_loss_labels(frame: pd.DataFrame, *, severe_loss_pct: float = -30.0) -> pd.Series:
    returns = pd.to_numeric(frame.get("target_total_pnl_pct", pd.Series(np.nan, index=frame.index)), errors="coerce")
    returns = returns.replace([np.inf, -np.inf], np.nan)
    return returns.le(float(severe_loss_pct)).astype("Int64").where(returns.notna())


def risk_summary(y_true: Any, y_prob: Any, threshold: float = 0.70) -> dict[str, Any]:
    y = np.asarray(y_true, dtype=int)
    p = np.asarray(y_prob, dtype=float)
    selected = p >= float(threshold)
    return {
        "rows": int(len(y)),
        "severe_losses": int(y.sum()),
        "threshold": float(threshold),
        "veto_count": int(selected.sum()),
        "veto_severe_losses": int(((selected == 1) & (y == 1)).sum()),
    }


__all__ = ["severe_loss_labels", "risk_summary"]
