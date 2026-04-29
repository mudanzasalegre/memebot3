from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analytics.report_utils import fnum, mcap_bucket, price5m_bucket, rank_bucket


@dataclass(frozen=True)
class BucketHealth:
    key: str
    trades: int
    avg_pnl_pct: float
    win_rate: float
    positive: bool


def bucket_keys(row: dict[str, Any]) -> dict[str, str]:
    lane = str(row.get("entry_lane") or "unknown")
    gate = str(row.get("gate_profile") or "unknown")
    proxy = str(bool(row.get("liquidity_is_proxy") or row.get("liquidity_usd_is_proxy"))).lower()
    return {
        "regime": str(row.get("entry_regime") or "unknown"),
        "lane": lane,
        "gate_profile": gate,
        "rank_bucket": rank_bucket(row.get("rank_score") or row.get("research_rank_score")),
        "price5m_bucket": price5m_bucket(row.get("price_pct_5m")),
        "mcap_bucket": mcap_bucket(row.get("market_cap_usd")),
        "liquidity_proxy": proxy,
        "lane_gate": f"{lane}|{gate}",
    }


def summarize_bucket(key: str, rows: list[dict[str, Any]]) -> BucketHealth:
    pnls = [fnum(row.get("pnl_pct") or row.get("realized_pnl_pct") or row.get("target_total_pnl_pct"), 0.0) for row in rows]
    if not pnls:
        return BucketHealth(key, 0, 0.0, 0.0, False)
    avg = sum(pnls) / len(pnls)
    win = 100.0 * sum(1 for value in pnls if value > 0) / len(pnls)
    return BucketHealth(key, len(pnls), round(avg, 3), round(win, 3), avg > 0)


__all__ = ["BucketHealth", "bucket_keys", "summarize_bucket"]
