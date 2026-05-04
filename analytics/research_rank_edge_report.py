from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any, Callable

from analytics.lane_policy_categories import (
    POLICY_PUMP_EARLY_SNIPER_RESEARCH,
    POLICY_RESEARCH_RANK_CANARY,
    classify_policy_category,
)
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


def _pnl(row: dict[str, Any]) -> float:
    return fnum(row.get("realized_pnl_pct") or row.get("total_pnl_pct") or row.get("pnl_pct") or row.get("target_total_pnl_pct"), 0.0)


def _peak(row: dict[str, Any]) -> float:
    return fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct") or row.get("max_pnl_pct"), _pnl(row))


def _liquidity_bucket(value: Any) -> str:
    liquidity = fnum(value, 0.0)
    if liquidity <= 0:
        return "liquidity_missing"
    if liquidity < 2_000:
        return "liquidity_<2k"
    if liquidity < 5_000:
        return "liquidity_2k_5k"
    if liquidity < 10_000:
        return "liquidity_5k_10k"
    if liquidity < 25_000:
        return "liquidity_10k_25k"
    return "liquidity_25k+"


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_pnl(row) for row in rows]
    if not pnls:
        return {"trades": 0}
    return {
        "trades": len(rows),
        "win_rate": round(100.0 * sum(1 for value in pnls if value > 0) / len(pnls), 3),
        "avg_pnl": round(sum(pnls) / len(pnls), 3),
        "median_pnl": round(statistics.median(pnls), 3),
        "total_pnl": round(sum(pnls), 3),
        "severe_loss_count": sum(1 for row in rows if is_severe_exit(row)),
        "runner_50_count": sum(1 for row in rows if _peak(row) >= 50),
        "runner_100_count": sum(1 for row in rows if _peak(row) >= 100),
        "runner_300_count": sum(1 for row in rows if _peak(row) >= 300),
        "runner_500_count": sum(1 for row in rows if _peak(row) >= 500),
    }


def _group(rows: list[dict[str, Any]], name: str, value_fn: Callable[[dict[str, Any]], str]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(value_fn(row), []).append(row)
    return {key: _summary(value) for key, value in sorted(grouped.items())}


def build_research_rank_edge_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    all_rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    rows = [
        row
        for row in all_rows
        if classify_policy_category(row) in {POLICY_RESEARCH_RANK_CANARY, POLICY_PUMP_EARLY_SNIPER_RESEARCH}
    ]
    for row in rows:
        row["lane_policy_category"] = classify_policy_category(row)
    return {
        "summary": _summary(rows),
        "by_policy_category": _group(rows, "policy_category", lambda row: str(row.get("lane_policy_category") or "unknown")),
        "by_rank_bucket": _group(rows, "rank_bucket", lambda row: rank_bucket(row.get("rank_score") or row.get("research_rank_score"))),
        "by_mcap_bucket": _group(rows, "mcap_bucket", lambda row: mcap_bucket(row.get("market_cap_usd") or row.get("buy_market_cap_usd"))),
        "by_price5m_bucket": _group(rows, "price5m_bucket", lambda row: price5m_bucket(row.get("price_pct_5m") or row.get("buy_price_pct_5m"))),
        "by_liquidity_bucket": _group(rows, "liquidity_bucket", lambda row: _liquidity_bucket(row.get("liquidity_usd") or row.get("buy_liquidity_usd"))),
        "by_liquidity_proxy": _group(
            rows,
            "liquidity_proxy",
            lambda row: "proxy" if boolish(row.get("liquidity_is_proxy") or row.get("liquidity_usd_is_proxy") or row.get("buy_liquidity_is_proxy")) else "real",
        ),
    }


def write_research_rank_edge_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_research_rank_edge_report(root)
    write_json(metrics_dir(root) / "research_rank_edge_report.json", report)
    lines = ["# Research Rank Edge Report", "", "| Group | Trades | Win rate | Avg PnL | Severe | Runners >=300 |", "|---|---:|---:|---:|---:|---:|"]
    for section in ("by_policy_category", "by_rank_bucket", "by_mcap_bucket", "by_price5m_bucket", "by_liquidity_bucket", "by_liquidity_proxy"):
        for key, stats in report[section].items():
            lines.append(
                f"| {section}:{key} | {stats.get('trades', 0)} | {stats.get('win_rate', 0):.2f}% | "
                f"{stats.get('avg_pnl', 0):.2f}% | {stats.get('severe_loss_count', 0)} | {stats.get('runner_300_count', 0)} |"
            )
    write_markdown(root / "docs" / "RESEARCH_RANK_EDGE_REPORT.md", lines)
    return report


__all__ = ["build_research_rank_edge_report", "write_research_rank_edge_report"]
