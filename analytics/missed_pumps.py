from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from analytics.green_sniper_gate import evaluate_green_sniper
from config.config import PROJECT_ROOT


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if out != out:
            return default
        return out
    except Exception:
        return default


def _reason(row: dict[str, Any]) -> str:
    return str(
        row.get("rule_that_blocked")
        or row.get("reject_reason")
        or row.get("delay_reason")
        or row.get("shadow_reason")
        or row.get("reason")
        or row.get("stage")
        or "unknown"
    )


def build_missed_pumps(root: Path | None = None, *, min_pnl_pct: float = 100.0) -> list[dict[str, Any]]:
    root = root or PROJECT_ROOT
    metrics = root / "data" / "metrics"
    outcomes = _read_jsonl(metrics / "candidate_outcomes.jsonl")
    runtime = _read_jsonl(metrics / "runtime_events.jsonl")
    bought = {
        str(row.get("address") or row.get("mint") or "")
        for row in runtime
        if str(row.get("event") or row.get("action") or "").lower() in {"buy", "bought", "buy_ok"}
    }
    out: list[dict[str, Any]] = []
    for row in outcomes:
        addr = str(row.get("address") or row.get("mint") or "")
        if not addr or addr in bought:
            continue
        later = max(
            _float(row.get("max_pnl_pct")),
            _float(row.get("max_pnl_pct_seen")),
            _float(row.get("target_total_pnl_pct")),
            _float(row.get("peak_pnl_pct")),
            _float(row.get("price_pct_5m")),
        )
        if later < min_pnl_pct:
            continue
        decision = evaluate_green_sniper(dict(row), dry_run=True, live=False)
        out.append(
            {
                "address": addr,
                "symbol": row.get("symbol"),
                "source": row.get("source") or row.get("discovered_via"),
                "first_seen_at": row.get("first_seen_at") or row.get("timestamp") or row.get("ts_utc"),
                "age_at_seen": row.get("age_minutes") or row.get("age_min"),
                "price_pct_5m_at_seen": row.get("price_pct_5m"),
                "txns_5m_at_seen": row.get("txns_last_5m"),
                "liquidity_at_seen": row.get("liquidity_usd"),
                "mcap_at_seen": row.get("market_cap_usd"),
                "route_at_seen": row.get("has_jupiter_route"),
                "reject_reason": row.get("reject_reason"),
                "delay_reason": row.get("delay_reason"),
                "shadow_reason": row.get("shadow_reason") or row.get("reason"),
                "would_green_sniper_pass": decision.action == "buy",
                "rule_that_blocked": _reason(row),
                "green_sniper_reason": decision.reason,
                "later_max_pnl_pct": later,
            }
        )
    return sorted(out, key=lambda item: float(item["later_max_pnl_pct"]), reverse=True)


def write_missed_pumps_report(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or PROJECT_ROOT
    rows = build_missed_pumps(root)
    metrics_dir = root / "data" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "missed_pumps.json").write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    lines = ["# Missed Pumps Report", "", "| Address | Later max PnL | Rule blocked | Would green sniper pass |", "|---|---:|---|---|"]
    for row in rows[:50]:
        lines.append(
            f"| {str(row['address'])[:10]}... | {float(row['later_max_pnl_pct']):.2f}% | "
            f"{row['rule_that_blocked']} | {row['would_green_sniper_pass']} |"
        )
    (docs / "MISSED_PUMPS_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows


__all__ = ["build_missed_pumps", "write_missed_pumps_report"]
