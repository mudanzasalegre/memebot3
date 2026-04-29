from __future__ import annotations

from runtime.hot_queue import HotQueue


def test_hot_queue_overflow_drops_low_priority_first() -> None:
    queue = HotQueue(max_size=2, persist_events=False)
    queue.add({"address": "LOW", "price_pct_5m": 0, "txns_last_5m": 1, "liquidity_usd": 100}, source="dex")
    queue.add({"address": "HIGH", "price_pct_5m": 120, "txns_last_5m": 200, "liquidity_usd": 10000, "rank_score": 80}, source="pumpportal")
    queue.add({"address": "MID", "price_pct_5m": 40, "txns_last_5m": 50, "liquidity_usd": 3000}, source="pumpfun")
    addresses = {item["address"] for item in queue.pop_batch(5)}
    assert "HIGH" in addresses
    assert "LOW" not in addresses
    assert queue.snapshot()["drop_counts"]["dropped_low_priority"] >= 1
