from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from analytics.lane_policy_categories import (
    POLICY_GREEN_SNIPER_PURE,
    POLICY_GREEN_SNIPER_RESTRICTED_BUY,
    POLICY_GREEN_SNIPER_SHADOW,
    classify_policy_category,
)
from analytics.report_utils import (
    boolish,
    fnum,
    is_severe_exit,
    load_candidate_outcomes,
    load_paper_positions,
    load_sqlite_positions,
    metrics_dir,
    write_json,
    write_markdown,
)
from config.config import CFG, PROJECT_ROOT


def _pnl(row: dict[str, Any]) -> float:
    return fnum(row.get("realized_pnl_pct") or row.get("total_pnl_pct") or row.get("pnl_pct") or row.get("target_total_pnl_pct"), 0.0)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_pnl(row) for row in rows]
    if not pnls:
        return {"trades": 0}
    return {
        "trades": len(rows),
        "win_rate": round(100.0 * sum(1 for value in pnls if value > 0) / len(pnls), 3),
        "avg_pnl": round(sum(pnls) / len(pnls), 3),
        "median_pnl": round(statistics.median(pnls), 3),
        "severe_loss_count": sum(1 for row in rows if is_severe_exit(row)),
    }


def restricted_failures(row: dict[str, Any]) -> list[str]:
    rank = fnum(row.get("rank_score") or row.get("research_rank_score"), 0.0)
    txns = fnum(row.get("txns_last_5m") or row.get("buy_txns_last_5m"), 0.0)
    liquidity = fnum(row.get("liquidity_usd") or row.get("buy_liquidity_usd"), 0.0)
    mcap = fnum(row.get("market_cap_usd") or row.get("buy_market_cap_usd"), 0.0)
    price5m = fnum(row.get("price_pct_5m") or row.get("buy_price_pct_5m"), -1.0)
    impact = fnum(row.get("price_impact_pct") or row.get("buy_price_impact_pct"), 0.0)
    has_route = boolish(row.get("has_jupiter_route"))
    proxy = boolish(row.get("liquidity_is_proxy") or row.get("liquidity_usd_is_proxy") or row.get("buy_liquidity_is_proxy"))
    risk = str(row.get("green_sniper_risk_level") or row.get("risk_level") or "low").lower()
    liq_risk = str(row.get("liquidity_risk_level") or "low").lower()

    failures: list[str] = []
    if rank < float(getattr(CFG, "GREEN_SNIPER_RESTRICTED_MIN_RANK", 64.0) or 64.0):
        failures.append("rank")
    if txns < float(getattr(CFG, "GREEN_SNIPER_RESTRICTED_MIN_TXNS", 300) or 300):
        failures.append("txns")
    if liquidity <= float(getattr(CFG, "GREEN_SNIPER_RESTRICTED_MIN_LIQUIDITY", 10_000.0) or 10_000.0):
        failures.append("liquidity")
    if not (
        float(getattr(CFG, "GREEN_SNIPER_RESTRICTED_MIN_MCAP", 25_000.0) or 25_000.0)
        <= mcap
        <= float(getattr(CFG, "GREEN_SNIPER_RESTRICTED_MAX_MCAP", 100_000.0) or 100_000.0)
    ):
        failures.append("mcap")
    if not (
        float(getattr(CFG, "GREEN_SNIPER_RESTRICTED_MIN_PRICE5M", 25.0) or 25.0)
        <= price5m
        <= float(getattr(CFG, "GREEN_SNIPER_RESTRICTED_MAX_PRICE5M", 100.0) or 100.0)
    ):
        failures.append("price5m")
    if bool(getattr(CFG, "GREEN_SNIPER_RESTRICTED_REQUIRE_ROUTE", True)) and not has_route:
        failures.append("route")
    if proxy:
        failures.append("proxy_liquidity")
    if impact > float(getattr(CFG, "GREEN_SNIPER_RESTRICTED_MAX_PRICE_IMPACT_PCT", 12.0) or 12.0):
        failures.append("impact")
    if risk != "low":
        failures.append("risk")
    if liq_risk != "low":
        failures.append("liquidity_risk")
    return failures


def build_green_sniper_restricted_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    all_rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    rows = [
        row
        for row in all_rows
        if classify_policy_category(row)
        in {POLICY_GREEN_SNIPER_PURE, POLICY_GREEN_SNIPER_RESTRICTED_BUY, POLICY_GREEN_SNIPER_SHADOW}
    ]
    eligible: list[dict[str, Any]] = []
    ineligible: list[dict[str, Any]] = []
    failure_counts: dict[str, int] = {}
    by_category: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        category = classify_policy_category(row)
        by_category.setdefault(category, []).append(row)
        failures = restricted_failures(row)
        if failures:
            ineligible.append(row)
            for failure in failures:
                failure_counts[failure] = failure_counts.get(failure, 0) + 1
        else:
            eligible.append(row)
    return {
        "summary": _summary(rows),
        "restricted_eligible": _summary(eligible),
        "restricted_ineligible": _summary(ineligible),
        "failure_counts": dict(sorted(failure_counts.items())),
        "by_policy_category": {key: _summary(value) for key, value in sorted(by_category.items())},
    }


def write_green_sniper_restricted_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_green_sniper_restricted_report(root)
    write_json(metrics_dir(root) / "green_sniper_restricted_report.json", report)
    lines = ["# Green Sniper Restricted Report", "", "| Group | Trades | Win rate | Avg PnL | Severe |", "|---|---:|---:|---:|---:|"]
    for key in ("summary", "restricted_eligible", "restricted_ineligible"):
        stats = report[key]
        lines.append(
            f"| {key} | {stats.get('trades', 0)} | {stats.get('win_rate', 0):.2f}% | "
            f"{stats.get('avg_pnl', 0):.2f}% | {stats.get('severe_loss_count', 0)} |"
        )
    lines.extend(["", "## Failure Counts", ""])
    for key, value in report["failure_counts"].items():
        lines.append(f"- `{key}`: `{value}`")
    write_markdown(root / "docs" / "GREEN_SNIPER_RESTRICTED_REPORT.md", lines)
    return report


__all__ = ["build_green_sniper_restricted_report", "restricted_failures", "write_green_sniper_restricted_report"]
