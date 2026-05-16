from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


REPORT_JSON = "pumpswap_prime_strict_report.json"
REPORT_MD = "PUMPSWAP_PRIME_STRICT.md"


@dataclass(frozen=True)
class PumpswapPrimeStrictDecision:
    allowed: bool
    failures: tuple[str, ...]
    block_reason: str


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _dex_id(row: dict[str, Any]) -> str:
    return str(_first(row, "buy_dex_id", "dex_id", "dexId") or "").strip().lower().replace("_", "").replace("-", "")


def _route_ok(row: dict[str, Any]) -> bool:
    return boolish(_first(row, "has_jupiter_route", "route_ok", "route_available"), False)


def liquidity_is_proxy(row: dict[str, Any]) -> bool:
    return boolish(_first(row, "buy_liquidity_is_proxy", "liquidity_is_proxy", "liquidity_usd_is_proxy"), False)


def txns_5m(row: dict[str, Any]) -> float:
    return fnum(_first(row, "buy_txns_last_5m", "txns_last_5m", "txns_5m"), 0.0)


def liquidity_usd(row: dict[str, Any]) -> float:
    return fnum(_first(row, "buy_liquidity_usd", "liquidity_usd"), 0.0)


def price_impact_pct(row: dict[str, Any]) -> float:
    return max(0.0, fnum(_first(row, "buy_price_impact_pct", "price_impact_pct"), 0.0))


def is_pumpswap_prime(row: dict[str, Any]) -> bool:
    gate = str(_first(row, "gate_profile", "sniper_gate_profile", "live_profit_gate_profile") or "").strip().lower()
    tier = str(_first(row, "profit_lane_tier", "size_bucket") or "").strip().lower()
    lane = str(_first(row, "entry_lane", "lane") or "").strip().lower()
    return (
        gate == "pumpswap_profit_prime"
        or tier in {"pump_early_pumpswap_prime", "pumpswap_prime"}
        or (lane == "pump_early_pumpswap_profit" and gate == "pumpswap_profit_prime")
    )


def evaluate_pumpswap_prime_strict(row: dict[str, Any], *, cfg: Any = CFG) -> PumpswapPrimeStrictDecision:
    if not bool(getattr(cfg, "PUMPSWAP_PRIME_STRICT_ENABLED", True)):
        return PumpswapPrimeStrictDecision(True, (), "")

    failures: list[str] = []
    min_txns = float(getattr(cfg, "PUMPSWAP_PRIME_MIN_TXNS_5M", 500) or 500)
    min_liq = float(getattr(cfg, "PUMPSWAP_PRIME_MIN_LIQUIDITY_USD", 10_000.0) or 10_000.0)
    max_impact = float(getattr(cfg, "PUMPSWAP_PRIME_MAX_PRICE_IMPACT_PCT", 12.0) or 12.0)

    if txns_5m(row) < min_txns:
        failures.append(f"txns5m<{min_txns:g}")
    if liquidity_usd(row) < min_liq:
        failures.append(f"liq<{min_liq:g}")
    if bool(getattr(cfg, "PUMPSWAP_PRIME_REQUIRE_REAL_LIQUIDITY", True)) and liquidity_is_proxy(row):
        failures.append("proxy_liquidity")
    if bool(getattr(cfg, "PUMPSWAP_PRIME_REQUIRE_ROUTE", True)) and not _route_ok(row):
        failures.append("route_required")
    if price_impact_pct(row) > max_impact:
        failures.append(f"impact>{max_impact:g}")

    if failures:
        return PumpswapPrimeStrictDecision(False, tuple(failures), "pumpswap_prime_strict_failed:" + ",".join(failures))
    return PumpswapPrimeStrictDecision(True, (), "")


def _pnl(row: dict[str, Any]) -> float:
    return fnum(
        _first(row, "realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct"),
        0.0,
    )


def _peak(row: dict[str, Any]) -> float:
    return max(
        fnum(_first(row, "max_pnl_pct_seen", "highest_pnl_pct", "peak_pnl_pct", "max_pnl_pct"), _pnl(row)),
        _pnl(row),
    )


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_pnl(row) for row in rows]
    if not pnls:
        return {
            "count": 0,
            "win_rate_pct": 0.0,
            "avg_pnl_pct": 0.0,
            "median_pnl_pct": 0.0,
            "total_pnl_pct_points": 0.0,
            "severe_loss_count": 0,
            "adverse_tick_count": 0,
            "liquidity_crush_count": 0,
            "runner_100_count": 0,
            "runner_500_count": 0,
        }
    return {
        "count": len(rows),
        "win_rate_pct": round(100.0 * sum(1 for value in pnls if value > 0) / len(pnls), 3),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        "median_pnl_pct": round(statistics.median(pnls), 3),
        "total_pnl_pct_points": round(sum(pnls), 3),
        "severe_loss_count": sum(1 for row, pnl in zip(rows, pnls) if is_severe_exit(row) or pnl <= -25.0),
        "adverse_tick_count": sum(1 for row in rows if str(_first(row, "exit_reason", "reason") or "").upper() == "ADVERSE_TICK"),
        "liquidity_crush_count": sum(1 for row in rows if str(_first(row, "exit_reason", "reason") or "").upper() == "LIQUIDITY_CRUSH"),
        "runner_100_count": sum(1 for row in rows if _peak(row) >= 100.0),
        "runner_500_count": sum(1 for row in rows if _peak(row) >= 500.0),
    }


def build_pumpswap_prime_strict_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    prime_rows = [row for row in rows if is_pumpswap_prime(row)]
    passed = [row for row in prime_rows if evaluate_pumpswap_prime_strict(row).allowed]
    blocked = [row for row in prime_rows if not evaluate_pumpswap_prime_strict(row).allowed]
    by_reason: dict[str, int] = {}
    for row in blocked:
        decision = evaluate_pumpswap_prime_strict(row)
        for reason in decision.failures:
            by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "config": {
            "enabled": bool(getattr(CFG, "PUMPSWAP_PRIME_STRICT_ENABLED", True)),
            "min_txns_5m": float(getattr(CFG, "PUMPSWAP_PRIME_MIN_TXNS_5M", 500) or 500),
            "min_liquidity_usd": float(getattr(CFG, "PUMPSWAP_PRIME_MIN_LIQUIDITY_USD", 10_000.0) or 10_000.0),
            "require_real_liquidity": bool(getattr(CFG, "PUMPSWAP_PRIME_REQUIRE_REAL_LIQUIDITY", True)),
            "require_route": bool(getattr(CFG, "PUMPSWAP_PRIME_REQUIRE_ROUTE", True)),
            "max_price_impact_pct": float(getattr(CFG, "PUMPSWAP_PRIME_MAX_PRICE_IMPACT_PCT", 12.0) or 12.0),
        },
        "previous_prime": _summary(prime_rows),
        "strict_passed": _summary(passed),
        "strict_blocked": _summary(blocked),
        "blocked_by_reason": dict(sorted(by_reason.items())),
        "runner_missed_by_blocking": {
            "peak_100_count": sum(1 for row in blocked if _peak(row) >= 100.0),
            "peak_500_count": sum(1 for row in blocked if _peak(row) >= 500.0),
        },
    }


def write_pumpswap_prime_strict_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_pumpswap_prime_strict_report(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    lines = [
        "# Pumpswap Prime Strict",
        "",
        "| Group | Count | Win | Avg PnL | Median PnL | Severe | ADVERSE_TICK | LIQUIDITY_CRUSH | >=100 | >=500 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, key in (("Previous prime", "previous_prime"), ("Strict passed", "strict_passed"), ("Strict blocked", "strict_blocked")):
        stats = report[key]
        lines.append(
            f"| {label} | {stats['count']} | {stats['win_rate_pct']:.2f}% | {stats['avg_pnl_pct']:.2f}% | "
            f"{stats['median_pnl_pct']:.2f}% | {stats['severe_loss_count']} | {stats['adverse_tick_count']} | "
            f"{stats['liquidity_crush_count']} | {stats['runner_100_count']} | {stats['runner_500_count']} |"
        )
    lines.extend(["", "## Blocked Reasons", ""])
    for reason, count in report["blocked_by_reason"].items():
        lines.append(f"- `{reason}`: {count}")
    write_markdown(root / "docs" / REPORT_MD, lines)
    return report


__all__ = [
    "PumpswapPrimeStrictDecision",
    "build_pumpswap_prime_strict_report",
    "evaluate_pumpswap_prime_strict",
    "is_pumpswap_prime",
    "liquidity_is_proxy",
    "liquidity_usd",
    "price_impact_pct",
    "txns_5m",
    "write_pumpswap_prime_strict_report",
]
