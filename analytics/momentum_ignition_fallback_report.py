from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from analytics.report_utils import (
    address_of,
    fnum,
    load_candidate_outcomes,
    load_paper_positions,
    load_runtime_events,
    load_sqlite_positions,
    metrics_dir,
    write_json,
)
from analytics.sniper_research_subprofiles import (
    SUBPROFILE_MOMENTUM_IGNITION,
    evaluate_sniper_research_subprofile,
    momentum_trend_missing_strong_reasons,
)
from config.config import CFG, PROJECT_ROOT


REPORT_JSON = "momentum_ignition_fallback_report.json"


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _is_sniper_research(row: dict[str, Any]) -> bool:
    return str(_first(row, "entry_lane", "lane") or "").strip().lower() == "pump_early_sniper_research"


def build_momentum_ignition_fallback_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_runtime_events(root) + load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    sniper_rows = [row for row in rows if _is_sniper_research(row)]
    fallback_allowed: list[dict[str, Any]] = []
    needs_confirmation: list[dict[str, Any]] = []
    hard_shadow: list[dict[str, Any]] = []
    strong_counts: dict[str, int] = {}
    for row in sniper_rows:
        decision = evaluate_sniper_research_subprofile(row)
        strong = momentum_trend_missing_strong_reasons(row)
        for reason in strong:
            strong_counts[reason] = strong_counts.get(reason, 0) + 1
        if decision.allowed and decision.subprofile == SUBPROFILE_MOMENTUM_IGNITION and strong:
            fallback_allowed.append(row)
        elif decision.reason == "momentum_ignition_needs_confirmation":
            needs_confirmation.append(row)
        elif "momentum:cluster_bad" in decision.failures or "momentum:toxic_initial_sell_pressure" in decision.failures:
            hard_shadow.append(row)
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "allow_trend_missing_if_strong": bool(
                getattr(CFG, "SNIPER_RESEARCH_MOMENTUM_ALLOW_TREND_MISSING_IF_STRONG", True)
            ),
            "strong_min_txns_5m": int(getattr(CFG, "SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_TXNS_5M", 1200) or 1200),
            "strong_min_rank": float(getattr(CFG, "SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_RANK", 70.0) or 70.0),
            "strong_min_liquidity": float(
                getattr(CFG, "SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_LIQUIDITY", 25_000.0) or 25_000.0
            ),
            "strong_min_volume_24h": float(
                getattr(CFG, "SNIPER_RESEARCH_MOMENTUM_STRONG_MIN_VOLUME_24H", 100_000.0) or 100_000.0
            ),
        },
        "summary": {
            "sniper_research_rows": len(sniper_rows),
            "fallback_allowed": len(fallback_allowed),
            "needs_confirmation": len(needs_confirmation),
            "cluster_or_toxic_shadow": len(hard_shadow),
        },
        "strong_condition_counts": dict(sorted(strong_counts.items())),
        "samples": [
            {
                "address": address_of(row),
                "price5m": fnum(_first(row, "buy_price_pct_5m", "price_pct_5m"), 0.0),
                "txns5m": fnum(_first(row, "buy_txns_last_5m", "txns_last_5m"), 0.0),
                "rank_score": _first(row, "rank_score", "research_rank_score"),
                "liquidity_usd": fnum(_first(row, "buy_liquidity_usd", "liquidity_usd"), 0.0),
                "strong_reasons": momentum_trend_missing_strong_reasons(row),
            }
            for row in fallback_allowed[:50]
        ],
    }


def write_momentum_ignition_fallback_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_momentum_ignition_fallback_report(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    return report


__all__ = ["REPORT_JSON", "build_momentum_ignition_fallback_report", "write_momentum_ignition_fallback_report"]
