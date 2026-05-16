from __future__ import annotations

import datetime as dt
import statistics
from pathlib import Path
from typing import Any

from analytics.lane_policy_categories import classify_policy_category
from analytics.report_utils import (
    boolish,
    fnum,
    is_severe_exit,
    load_candidate_outcomes,
    load_paper_positions,
    load_sqlite_positions,
    mcap_bucket,
    metrics_dir,
    price5m_bucket,
    rank_bucket,
    write_json,
    write_markdown,
)
from config.config import PROJECT_ROOT
from ml.data_contract import normalize_ml_row


GROUP_FIELDS = (
    "entry_lane",
    "lane_policy_category",
    "gate_profile",
    "entry_subtype",
    "green_sniper_reason",
    "exit_reason",
    "sample_type",
    "rank_bucket",
    "price5m_bucket",
    "mcap_bucket",
    "liquidity_proxy",
    "has_jupiter_route",
)


def _pnl(row: dict[str, Any]) -> float:
    return fnum(row.get("realized_pnl_pct") or row.get("total_pnl_pct") or row.get("pnl_pct") or row.get("target_total_pnl_pct"), 0.0)


def _normalize(row: dict[str, Any], sample_type: str | None = None) -> dict[str, Any]:
    out = normalize_ml_row(row)
    if sample_type and out.get("sample_type") == "unknown":
        out["sample_type"] = sample_type
    out["pnl_pct"] = _pnl(row)
    out["exit_reason"] = str(row.get("exit_reason") or row.get("reason") or "")
    out["green_sniper_reason"] = str(row.get("green_sniper_reason") or row.get("sniper_reason") or "")
    out["lane_policy_category"] = classify_policy_category(out)
    out["rank_bucket"] = rank_bucket(row.get("rank_score") or row.get("research_rank_score"))
    out["price5m_bucket"] = price5m_bucket(row.get("price_pct_5m") or row.get("buy_price_pct_5m"))
    out["mcap_bucket"] = mcap_bucket(row.get("market_cap_usd") or row.get("buy_market_cap_usd"))
    out["liquidity_proxy"] = boolish(row.get("liquidity_is_proxy") or row.get("liquidity_usd_is_proxy"))
    out["has_jupiter_route"] = boolish(row.get("has_jupiter_route"))
    out["max_pnl"] = fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct") or row.get("max_pnl_pct"), out["pnl_pct"])
    return out


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_pnl(row) for row in rows]
    if not pnls:
        return {}
    return {
        "trades": len(rows),
        "win_rate": round(100.0 * sum(1 for value in pnls if value > 0) / len(pnls), 3),
        "avg_pnl": round(sum(pnls) / len(pnls), 3),
        "median_pnl": round(statistics.median(pnls), 3),
        "total_pnl_points": round(sum(pnls), 3),
        "max_pnl": round(max(pnls), 3),
        "min_pnl": round(min(pnls), 3),
        "severe_loss_count": sum(1 for row in rows if is_severe_exit(row)),
        "runner_count_50": sum(1 for row in rows if fnum(row.get("max_pnl"), _pnl(row)) >= 50),
        "runner_count_100": sum(1 for row in rows if fnum(row.get("max_pnl"), _pnl(row)) >= 100),
        "runner_count_300": sum(1 for row in rows if fnum(row.get("max_pnl"), _pnl(row)) >= 300),
        "runner_count_500": sum(1 for row in rows if fnum(row.get("max_pnl"), _pnl(row)) >= 500),
        "adverse_tick_count": sum(1 for row in rows if str(row.get("exit_reason")).upper() == "ADVERSE_TICK"),
        "liq_crush_count": sum(1 for row in rows if str(row.get("exit_reason")).upper() == "LIQUIDITY_CRUSH"),
    }


def build_trade_diagnostics(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows: list[dict[str, Any]] = []
    rows.extend(_normalize(row, "shadow_close") for row in load_candidate_outcomes(root))
    rows.extend(_normalize(row, "trade_close") for row in load_paper_positions(root))
    rows.extend(_normalize(row, "trade_close") for row in load_sqlite_positions(root))
    rows = [row for row in rows if row.get("pnl_pct") is not None]

    groups: dict[str, list[dict[str, Any]]] = {}
    for field in GROUP_FIELDS:
        for row in rows:
            key = f"{field}:{row.get(field)}"
            groups.setdefault(key, []).append(row)

    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "summary": _summarize(rows),
        "groups": {key: _summarize(value) for key, value in sorted(groups.items()) if value},
    }


def write_trade_diagnostics_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_trade_diagnostics(root)
    write_json(metrics_dir(root) / "trade_diagnostics.json", report)
    lines = ["# Trade Diagnostics", "", "| Group | Trades | Win rate | Avg PnL | Median PnL | Severe |", "|---|---:|---:|---:|---:|---:|"]
    for key, stats in sorted(report["groups"].items(), key=lambda item: item[1].get("total_pnl_points", 0), reverse=True)[:150]:
        lines.append(
            f"| {key} | {stats.get('trades', 0)} | {stats.get('win_rate', 0):.2f}% | "
            f"{stats.get('avg_pnl', 0):.2f}% | {stats.get('median_pnl', 0):.2f}% | {stats.get('severe_loss_count', 0)} |"
        )
    write_markdown(root / "docs" / "TRADE_DIAGNOSTICS.md", lines)
    return report


__all__ = ["build_trade_diagnostics", "write_trade_diagnostics_report"]
