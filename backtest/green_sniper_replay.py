from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from statistics import mean, median
from typing import Any

from config.config import PROJECT_ROOT


def _rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT entry_lane, gate_profile, size_bucket, buy_dex_id, buy_liquidity_is_proxy, "
                "buy_liquidity_usd, buy_market_cap_usd, buy_price_pct_5m, buy_txns_last_5m, "
                "total_pnl_pct, total_pnl_usd, exit_reason FROM positions WHERE closed=1"
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _policy_match(row: dict[str, Any], policy: str) -> bool:
    lane = str(row.get("entry_lane") or "").lower()
    profile = str(row.get("gate_profile") or "").lower()
    dex = str(row.get("buy_dex_id") or "").lower()
    price5m = _f(row.get("buy_price_pct_5m"))
    txns = _f(row.get("buy_txns_last_5m"))
    liq = _f(row.get("buy_liquidity_usd"))
    mcap = _f(row.get("buy_market_cap_usd"))
    proxy = bool(row.get("buy_liquidity_is_proxy"))
    if policy == "current_profit_lane":
        return lane == "pump_early_pumpswap_profit" or profile.startswith("pumpswap_profit")
    if policy == "current_breakout_probe":
        return lane == "pump_early_pumpswap_breakout_probe" or profile.startswith("pumpswap_breakout")
    if policy == "green_sniper_conservative":
        return dex == "pumpswap" and not proxy and liq >= 2500 and 20 <= price5m <= 180 and txns >= 60 and 2000 <= mcap <= 180000
    if policy == "green_sniper_aggressive":
        return liq >= 1200 and 20 <= price5m <= 280 and txns >= 35 and 2000 <= mcap <= 180000
    if policy == "green_sniper_live_canary":
        return dex == "pumpswap" and not proxy and liq >= 2500 and 20 <= price5m <= 180 and txns >= 60
    return False


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_f(row.get("total_pnl_pct")) for row in rows]
    severe = sum(1 for row in rows if str(row.get("exit_reason") or "").upper() in {"LIQUIDITY_CRUSH", "STOP_LOSS", "EARLY_DROP", "ADVERSE_TICK"} or _f(row.get("total_pnl_pct")) <= -25)
    return {
        "trades": len(rows),
        "win_rate": (sum(1 for v in pnls if v > 0) / len(pnls) * 100.0) if pnls else 0.0,
        "avg_pnl": mean(pnls) if pnls else 0.0,
        "median_pnl": median(pnls) if pnls else 0.0,
        "total_pnl": sum(_f(row.get("total_pnl_usd")) for row in rows),
        "max_pnl": max(pnls) if pnls else 0.0,
        "trades_gt_100": sum(1 for v in pnls if v >= 100),
        "trades_gt_300": sum(1 for v in pnls if v >= 300),
        "severe_losses": severe,
        "missed_jackpot_count": 0,
        "runner_capture_rate": None,
    }


def replay_green_sniper(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = _rows(root / "data" / "memebotdatabase.db")
    policies = [
        "current_profit_lane",
        "current_breakout_probe",
        "green_sniper_conservative",
        "green_sniper_aggressive",
        "green_sniper_live_canary",
    ]
    return {policy: _metrics([row for row in rows if _policy_match(row, policy)]) for policy in policies}


def write_green_sniper_replay(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = replay_green_sniper(root)
    metrics = root / "data" / "metrics"
    metrics.mkdir(parents=True, exist_ok=True)
    (metrics / "green_sniper_replay.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    lines = ["# Green Sniper Replay", "", "| Policy | Trades | Win rate | Avg PnL | Median PnL | Total USD |", "|---|---:|---:|---:|---:|---:|"]
    for policy, m in report.items():
        lines.append(
            f"| {policy} | {m['trades']} | {m['win_rate']:.2f}% | {m['avg_pnl']:.2f}% | "
            f"{m['median_pnl']:.2f}% | {m['total_pnl']:.2f} |"
        )
    (docs / "GREEN_SNIPER_REPLAY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


__all__ = ["replay_green_sniper", "write_green_sniper_replay"]
