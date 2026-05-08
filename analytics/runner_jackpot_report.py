from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from config.config import PROJECT_ROOT


DEFAULT_OUTCOMES = PROJECT_ROOT / "data" / "metrics" / "candidate_outcomes.jsonl"
DEFAULT_DB = PROJECT_ROOT / "data" / "memebotdatabase.db"
DEFAULT_JSON = PROJECT_ROOT / "data" / "metrics" / "runner_jackpot_report.json"
DEFAULT_MD = PROJECT_ROOT / "docs" / "RUNNER_JACKPOT_REPORT.md"


def _fnum(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        out = float(value)
        return float(default) if out != out else out
    except Exception:
        return float(default)


def _s(value: Any) -> str:
    return str(value or "").strip()


def _peak(row: dict[str, Any]) -> float:
    return max(
        _fnum(row.get("max_pnl_seen")),
        _fnum(row.get("max_pnl_pct_seen")),
        _fnum(row.get("highest_pnl_pct")),
        _fnum(row.get("peak_pnl_pct")),
        _fnum(row.get("total_pnl_pct")),
        _fnum(row.get("pnl_pct")),
    )


def _pnl(row: dict[str, Any]) -> float:
    return _fnum(row.get("total_pnl_pct"), _fnum(row.get("pnl_pct")))


def _bucket(row: dict[str, Any]) -> str:
    lane = _s(row.get("entry_lane")) or "unknown"
    gate = _s(row.get("gate_profile") or row.get("sniper_gate_profile"))
    tier = _s(row.get("profit_lane_tier") or row.get("lane_policy_category") or row.get("policy_category"))
    if tier:
        return tier
    if gate:
        return f"{lane}:{gate}"
    return lane


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _load_positions(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT id, symbol, address, entry_lane, gate_profile, runner_exit_profile,
                   buy_dex_id, buy_liquidity_is_proxy, buy_liquidity_usd,
                   buy_market_cap_usd, buy_price_pct_5m, buy_txns_last_5m,
                   highest_pnl_pct, max_pnl_pct_seen, total_pnl_pct, exit_reason,
                   partial_taken, exit_from_peak_giveback_pct
            FROM positions
            """
        ).fetchall()
        return [dict(row) | {"source_table": "positions"} for row in rows]
    except Exception:
        return []


def _summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    vals = list(rows)
    pnls = [_pnl(row) for row in vals]
    peaks = [_peak(row) for row in vals]
    return {
        "count": len(vals),
        "win_rate": round(sum(1 for value in pnls if value > 0) / len(pnls) * 100.0, 3) if pnls else 0.0,
        "avg_pnl": round(sum(pnls) / len(pnls), 3) if pnls else 0.0,
        "median_pnl": round(float(median(pnls)), 3) if pnls else 0.0,
        "avg_peak": round(sum(peaks) / len(peaks), 3) if peaks else 0.0,
        "runner_300": sum(1 for value in peaks if value >= 300.0),
        "runner_500": sum(1 for value in peaks if value >= 500.0),
        "runner_1000": sum(1 for value in peaks if value >= 1000.0),
    }


def _top_rows(rows: Iterable[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    out = []
    for row in sorted(rows, key=_peak, reverse=True)[:limit]:
        out.append(
            {
                "symbol": _s(row.get("symbol")),
                "address": _s(row.get("address") or row.get("token_mint")),
                "peak_pnl_pct": round(_peak(row), 3),
                "realized_pnl_pct": round(_pnl(row), 3),
                "entry_lane": _s(row.get("entry_lane")),
                "gate_profile": _s(row.get("gate_profile") or row.get("sniper_gate_profile")),
                "profit_lane_tier": _s(row.get("profit_lane_tier") or row.get("lane_policy_category") or row.get("policy_category")),
                "exit_reason": _s(row.get("exit_reason")),
                "runner_exit_profile": _s(row.get("runner_exit_profile")),
                "price5m": _fnum(row.get("buy_price_pct_5m"), _fnum(row.get("price_pct_5m"), 0.0)),
                "mcap": _fnum(row.get("buy_market_cap_usd"), _fnum(row.get("market_cap_usd"), 0.0)),
                "liquidity": _fnum(row.get("buy_liquidity_usd"), _fnum(row.get("liquidity_usd"), 0.0)),
                "txns_5m": _fnum(row.get("buy_txns_last_5m"), _fnum(row.get("txns_last_5m"), 0.0)),
                "liquidity_proxy": bool(row.get("buy_liquidity_is_proxy") or row.get("liquidity_is_proxy") or row.get("liquidity_usd_is_proxy")),
            }
        )
    return out


def build_runner_jackpot_report(
    *,
    outcomes_path: Path = DEFAULT_OUTCOMES,
    db_path: Path = DEFAULT_DB,
) -> dict[str, Any]:
    rows = _load_jsonl(outcomes_path) + _load_positions(db_path)
    runners = [row for row in rows if _peak(row) >= 300.0]
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in runners:
        by_bucket[_bucket(row)].append(row)

    return {
        "summary": _summary(rows),
        "runners": _summary(runners),
        "by_lane_or_tier": {key: _summary(value) for key, value in sorted(by_bucket.items())},
        "top_runners": _top_rows(runners),
        "recommended_profile": {
            "name": "jackpot_runner",
            "target_pattern": "real-liquidity pumpswap/research-rank, mcap 50k-100k, price5m 25-100, txns >=500",
            "partial_fraction": 0.35,
            "step_locks": [
                {"peak": 100, "lock_floor": 80, "max_giveback": 18},
                {"peak": 300, "lock_floor": 180, "max_giveback": 25},
                {"peak": 500, "lock_floor": 320, "max_giveback": 35},
            ],
        },
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Runner Jackpot Report",
        "",
        "## Summary",
        "",
        f"- Rows analysed: {report['summary']['count']}",
        f"- Runners >=300: {report['runners']['runner_300']}",
        f"- Runners >=500: {report['runners']['runner_500']}",
        f"- Runners >=1000: {report['runners']['runner_1000']}",
        "",
        "## Top Runners",
        "",
        "| Symbol | Peak | Realized | Lane | Gate | Exit | Liquidity | Mcap | Price5m | Txns |",
        "|---|---:|---:|---|---|---|---:|---:|---:|---:|",
    ]
    for row in report["top_runners"][:15]:
        lines.append(
            f"| {row['symbol'] or row['address'][:6]} | {row['peak_pnl_pct']:.1f}% | "
            f"{row['realized_pnl_pct']:.1f}% | {row['entry_lane']} | {row['gate_profile']} | "
            f"{row['exit_reason']} | {row['liquidity']:.0f} | {row['mcap']:.0f} | "
            f"{row['price5m']:.1f} | {row['txns_5m']:.0f} |"
        )
    lines.extend(
        [
            "",
            "## Applied Policy",
            "",
            "The `jackpot_runner` profile is for the real-liquidity research-rank pattern that produced the largest confirmed runner.",
            "It sells a smaller first partial and then tightens lock floors after +100%, +300% and +500% peaks.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_runner_jackpot_report(
    *,
    json_path: Path = DEFAULT_JSON,
    md_path: Path = DEFAULT_MD,
    outcomes_path: Path = DEFAULT_OUTCOMES,
    db_path: Path = DEFAULT_DB,
) -> dict[str, Any]:
    report = build_runner_jackpot_report(outcomes_path=outcomes_path, db_path=db_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    return report


__all__ = ["build_runner_jackpot_report", "write_runner_jackpot_report"]
