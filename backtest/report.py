from __future__ import annotations

from typing import Any

import pandas as pd


def summarize_replay(frame: pd.DataFrame, selected) -> dict[str, Any]:
    pnl = pd.to_numeric(frame.get("target_total_pnl_pct"), errors="coerce")
    mask = pd.Series(selected, index=frame.index).astype(bool)
    selected_pnl = pnl[mask].dropna()
    jackpots = pnl.ge(100.0).fillna(False)
    severe = pnl.le(-30.0).fillna(False)
    return {
        "trades": int(mask.sum()),
        "win_rate": float(selected_pnl.gt(0).mean()) if len(selected_pnl) else None,
        "avg_pnl": float(selected_pnl.mean()) if len(selected_pnl) else None,
        "median_pnl": float(selected_pnl.median()) if len(selected_pnl) else None,
        "total_pnl": float(selected_pnl.sum()) if len(selected_pnl) else 0.0,
        "max_drawdown_approx": float(min(0.0, selected_pnl.cumsum().min())) if len(selected_pnl) else 0.0,
        "jackpot_capture_rate": float((mask & jackpots).sum() / jackpots.sum()) if int(jackpots.sum()) else None,
        "severe_loss_count": int((mask & severe).sum()),
        "rejected_winners": int((~mask & pnl.gt(0).fillna(False)).sum()),
        "accepted_losers": int((mask & pnl.lt(0).fillna(False)).sum()),
        "capital_weighted_pnl": float(selected_pnl.sum()) if len(selected_pnl) else 0.0,
    }


def render_report(report: dict[str, Any]) -> str:
    lines = ["# Backtest Report", ""]
    for key, value in report.items():
        lines.append(f"- `{key}`: `{value}`")
    lines.append("")
    return "\n".join(lines)


__all__ = ["summarize_replay", "render_report"]
