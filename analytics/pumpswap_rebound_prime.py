from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analytics.pumpswap_prime_strict import liquidity_is_proxy, liquidity_usd, price_impact_pct, txns_5m
from analytics.report_utils import (
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


LANE_PUMPSWAP_REBOUND_PRIME = "pump_early_pumpswap_rebound_prime"
GATE_PUMPSWAP_REBOUND_PRIME = "pumpswap_rebound_prime"
REPORT_JSON = "pumpswap_rebound_prime_report.json"
REPORT_MD = "PUMPSWAP_REBOUND_PRIME.md"


@dataclass(frozen=True)
class PumpswapReboundPrimeDecision:
    allowed: bool
    failures: tuple[str, ...]
    block_reason: str


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def dex_id(row: dict[str, Any]) -> str:
    return str(_first(row, "buy_dex_id", "dex_id", "dexId") or "").strip().lower().replace("_", "").replace("-", "")


def route_ok(row: dict[str, Any]) -> bool:
    return _boolish(_first(row, "has_jupiter_route", "route_ok", "route_available"))


def price5m_pct(row: dict[str, Any]) -> float | None:
    value = _first(row, "buy_price_pct_5m", "price_pct_5m", "price5m")
    if value is None:
        return None
    return fnum(value, 0.0)


def market_cap_usd(row: dict[str, Any]) -> float:
    return fnum(_first(row, "buy_market_cap_usd", "market_cap_usd", "mcap"), 0.0)


def evaluate_pumpswap_rebound_prime(row: dict[str, Any], *, cfg: Any = CFG) -> PumpswapReboundPrimeDecision:
    if not bool(getattr(cfg, "PUMPSWAP_REBOUND_PRIME_ENABLED", True)):
        return PumpswapReboundPrimeDecision(False, ("disabled",), "pumpswap_rebound_prime_failed:disabled")

    failures: list[str] = []
    max_price5m = float(getattr(cfg, "PUMPSWAP_REBOUND_PRIME_MAX_PRICE5M", -25.0) or -25.0)
    min_txns = float(getattr(cfg, "PUMPSWAP_REBOUND_PRIME_MIN_TXNS_5M", 500) or 500)
    min_liq = float(getattr(cfg, "PUMPSWAP_REBOUND_PRIME_MIN_LIQUIDITY_USD", 10_000.0) or 10_000.0)
    min_mcap = float(getattr(cfg, "PUMPSWAP_REBOUND_PRIME_MIN_MCAP_USD", 10_000.0) or 10_000.0)
    max_mcap = float(getattr(cfg, "PUMPSWAP_REBOUND_PRIME_MAX_MCAP_USD", 50_000.0) or 50_000.0)
    max_impact = float(getattr(cfg, "PUMPSWAP_REBOUND_PRIME_MAX_PRICE_IMPACT_PCT", 12.0) or 12.0)
    price5m = price5m_pct(row)
    mcap = market_cap_usd(row)

    if dex_id(row) != "pumpswap":
        failures.append("dex!=pumpswap")
    if price5m is None:
        failures.append("price5m_missing")
    elif price5m > max_price5m:
        failures.append(f"price5m>{max_price5m:g}")
    if txns_5m(row) < min_txns:
        failures.append(f"txns5m<{min_txns:g}")
    if liquidity_usd(row) < min_liq:
        failures.append(f"liq<{min_liq:g}")
    if mcap < min_mcap:
        failures.append(f"mcap<{min_mcap:g}")
    if mcap > max_mcap:
        failures.append(f"mcap>{max_mcap:g}")
    if bool(getattr(cfg, "PUMPSWAP_REBOUND_PRIME_REQUIRE_REAL_LIQUIDITY", True)) and liquidity_is_proxy(row):
        failures.append("proxy_liquidity")
    if bool(getattr(cfg, "PUMPSWAP_REBOUND_PRIME_REQUIRE_ROUTE", True)) and not route_ok(row):
        failures.append("route_required")
    if price_impact_pct(row) > max_impact:
        failures.append(f"impact>{max_impact:g}")

    if failures:
        return PumpswapReboundPrimeDecision(False, tuple(failures), "pumpswap_rebound_prime_failed:" + ",".join(failures))
    return PumpswapReboundPrimeDecision(True, (), "")


def apply_pumpswap_rebound_prime_context(row: dict[str, Any]) -> dict[str, Any]:
    row["entry_lane"] = LANE_PUMPSWAP_REBOUND_PRIME
    row["gate_profile"] = GATE_PUMPSWAP_REBOUND_PRIME
    row["sniper_gate_profile"] = GATE_PUMPSWAP_REBOUND_PRIME
    row["live_profit_gate_profile"] = GATE_PUMPSWAP_REBOUND_PRIME
    row["profit_lane_tier"] = LANE_PUMPSWAP_REBOUND_PRIME
    row["lane_policy_category"] = GATE_PUMPSWAP_REBOUND_PRIME
    row["pumpswap_rebound_prime"] = 1
    return row


def _pnl(row: dict[str, Any]) -> float:
    return fnum(_first(row, "realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct"), 0.0)


def _peak(row: dict[str, Any]) -> float:
    return max(fnum(_first(row, "max_pnl_pct_seen", "highest_pnl_pct", "peak_pnl_pct", "max_pnl_pct"), _pnl(row)), _pnl(row))


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_pnl(row) for row in rows]
    if not pnls:
        return {"count": 0, "win_rate_pct": 0.0, "avg_pnl_pct": 0.0, "median_pnl_pct": 0.0, "severe_loss_count": 0, "runner_100_count": 0, "runner_500_count": 0}
    return {
        "count": len(rows),
        "win_rate_pct": round(100.0 * sum(1 for value in pnls if value > 0) / len(pnls), 3),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        "median_pnl_pct": round(statistics.median(pnls), 3),
        "total_pnl_pct_points": round(sum(pnls), 3),
        "severe_loss_count": sum(1 for row, pnl in zip(rows, pnls) if is_severe_exit(row) or pnl <= -25.0),
        "runner_100_count": sum(1 for row in rows if _peak(row) >= 100.0),
        "runner_500_count": sum(1 for row in rows if _peak(row) >= 500.0),
    }


def build_pumpswap_rebound_prime_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    candidates = [row for row in rows if evaluate_pumpswap_rebound_prime(row).allowed]
    failures: dict[str, int] = {}
    for row in rows:
        decision = evaluate_pumpswap_rebound_prime(row)
        if decision.allowed:
            continue
        for reason in decision.failures:
            failures[reason] = failures.get(reason, 0) + 1
    return {
        "config": {
            "enabled": bool(getattr(CFG, "PUMPSWAP_REBOUND_PRIME_ENABLED", True)),
            "max_price5m": float(getattr(CFG, "PUMPSWAP_REBOUND_PRIME_MAX_PRICE5M", -25.0) or -25.0),
            "min_txns_5m": float(getattr(CFG, "PUMPSWAP_REBOUND_PRIME_MIN_TXNS_5M", 500) or 500),
            "min_liquidity_usd": float(getattr(CFG, "PUMPSWAP_REBOUND_PRIME_MIN_LIQUIDITY_USD", 10_000.0) or 10_000.0),
            "min_mcap_usd": float(getattr(CFG, "PUMPSWAP_REBOUND_PRIME_MIN_MCAP_USD", 10_000.0) or 10_000.0),
            "max_mcap_usd": float(getattr(CFG, "PUMPSWAP_REBOUND_PRIME_MAX_MCAP_USD", 50_000.0) or 50_000.0),
        },
        "candidates": _summary(candidates),
        "top_failures": dict(sorted(failures.items(), key=lambda item: item[1], reverse=True)[:20]),
    }


def write_pumpswap_rebound_prime_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_pumpswap_rebound_prime_report(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    stats = report["candidates"]
    lines = [
        "# Pumpswap Rebound Prime",
        "",
        "| Candidates | Win | Avg PnL | Median PnL | Severe | >=100 | >=500 |",
        "|---:|---:|---:|---:|---:|---:|---:|",
        f"| {stats['count']} | {stats['win_rate_pct']:.2f}% | {stats['avg_pnl_pct']:.2f}% | {stats['median_pnl_pct']:.2f}% | {stats['severe_loss_count']} | {stats['runner_100_count']} | {stats['runner_500_count']} |",
        "",
        "## Top Failures",
        "",
    ]
    for reason, count in report["top_failures"].items():
        lines.append(f"- `{reason}`: {count}")
    write_markdown(root / "docs" / REPORT_MD, lines)
    return report


__all__ = [
    "GATE_PUMPSWAP_REBOUND_PRIME",
    "LANE_PUMPSWAP_REBOUND_PRIME",
    "PumpswapReboundPrimeDecision",
    "apply_pumpswap_rebound_prime_context",
    "build_pumpswap_rebound_prime_report",
    "dex_id",
    "evaluate_pumpswap_rebound_prime",
    "market_cap_usd",
    "price5m_pct",
    "route_ok",
    "write_pumpswap_rebound_prime_report",
]
