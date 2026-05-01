from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from analytics.report_utils import fnum, load_candidate_outcomes, metrics_dir, mcap_bucket, price5m_bucket, rank_bucket, write_json
from config.config import PROJECT_ROOT


def _bucket_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("entry_lane") or "unknown"),
            price5m_bucket(row.get("price_pct_5m") or row.get("buy_price_pct_5m")),
            mcap_bucket(row.get("market_cap_usd") or row.get("buy_market_cap_usd")),
            str(bool(row.get("liquidity_is_proxy") or row.get("buy_liquidity_is_proxy"))).lower(),
            rank_bucket(row.get("rank_score") or row.get("research_rank_score")),
        ]
    )


def build_dynamic_thresholds(root: Path | None = None, *, min_samples: int = 10) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in load_candidate_outcomes(root):
        buckets[_bucket_key(row)].append(row)
    out: dict[str, Any] = {"min_samples": int(min_samples), "thresholds": {}}
    for key, rows in sorted(buckets.items()):
        pnls = [fnum(row.get("pnl_pct") or row.get("realized_pnl_pct") or row.get("target_total_pnl_pct"), 0.0) for row in rows]
        if len(rows) < min_samples:
            out["thresholds"][key] = {"sample_size": len(rows), "activation_ready": False}
            continue
        avg = sum(pnls) / len(pnls)
        severe = sum(1 for value in pnls if value <= -30) / len(pnls)
        out["thresholds"][key] = {
            "sample_size": len(rows),
            "activation_ready": True,
            "risk_max": round(max(0.15, min(0.85, 0.70 - severe * 0.5)), 4),
            "ev_min": round(max(0.0, -avg), 4),
            "runner_min": 0.10,
            "policy_score_min": round(max(0.0, 10.0 - avg), 4),
            "continuation_min": 0.0,
            "avg_pnl": round(avg, 4),
            "severe_rate": round(severe, 4),
        }
    return out


def write_dynamic_thresholds(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_dynamic_thresholds(root)
    write_json(metrics_dir(root) / "dynamic_thresholds.json", report)
    return report


__all__ = ["build_dynamic_thresholds", "write_dynamic_thresholds"]
