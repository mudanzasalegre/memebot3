from __future__ import annotations

from runtime.hot_queue import HotQueue
from runtime import loop_scheduler
import asyncio


def test_hot_queue_overflow_drops_low_priority_first() -> None:
    queue = HotQueue(max_size=2, persist_events=False)
    queue.add({"address": "LOW", "price_pct_5m": 0, "txns_last_5m": 1, "liquidity_usd": 100}, source="dex")
    queue.add({"address": "HIGH", "price_pct_5m": 120, "txns_last_5m": 200, "liquidity_usd": 10000, "rank_score": 80}, source="pumpportal")
    queue.add({"address": "MID", "price_pct_5m": 40, "txns_last_5m": 50, "liquidity_usd": 3000}, source="pumpfun")
    addresses = {item["address"] for item in queue.pop_batch(5)}
    assert "HIGH" in addresses
    assert "LOW" not in addresses
    assert queue.snapshot()["drop_counts"]["dropped_low_priority"] >= 1


def test_incremental_hot_service_keeps_tail_pending_and_uses_latest_snapshot():
    now = [1_000.]
    queue = HotQueue(max_size=20, max_age_min=20, persist_events=False)
    queue._now = lambda: now[0]
    for i in range(10):
        queue.add({"address": f"Q{i}", "price_pct_5m": 80-i, "txns_last_5m": 100,
                   "liquidity_usd": 20000, "age_minutes": 1}, source="pumpfun")
    calls = []
    async def evaluate(token):
        calls.append(token)
        now[0] += 20  # One slow entry, without any real sleep/provider call.
    count = asyncio.run(loop_scheduler.evaluate_hot_queue(queue, evaluate, max_items=10,
        budget_s=3, clock=lambda: now[0]))
    assert count == 1 and queue.snapshot()["size"] == 9
    assert len([event for event in queue.events() if event["event"] == "hot_queue_eval"]) == 1
    queue.add({"address": "Q9", "price_pct_5m": 500, "txns_last_5m": 800,
               "liquidity_usd": 30000, "age_minutes": 1}, source="pumpfun")
    asyncio.run(loop_scheduler.evaluate_hot_queue(queue, evaluate, max_items=1, budget_s=3, clock=lambda: now[0]))
    assert calls[1]["address"] == "Q9" and calls[1]["price_pct_5m"] == 500


def test_expiry_is_rechecked_at_actual_hot_pop_not_at_bulk_batch_start():
    now = [1_000.]
    queue = HotQueue(max_size=5, max_age_min=2, persist_events=False)
    queue._now = lambda: now[0]
    for i in range(3):
        queue.add({"address": f"Q{i}", "age_minutes": 1, "price_pct_5m": 100,
                   "txns_last_5m": 200, "liquidity_usd": 20000}, source="pumpfun")
    calls = []
    async def evaluate(token):
        calls.append(token)
        now[0] += 121
    asyncio.run(loop_scheduler.evaluate_hot_queue(queue, evaluate, max_items=3, budget_s=300, clock=lambda: now[0]))
    assert len(calls) == 1 and queue.snapshot()["size"] == 0
    assert len([event for event in queue.events()
                if event["event"] == "hot_queue_drop" and event["reason"] == "max_age"]) == 2


def test_incremental_hot_service_keeps_new_same_address_updates_eligible():
    snapshots = iter(({"address": "A", "price_pct_5m": 100},
                      {"address": "A", "price_pct_5m": 500}))
    class Queue:
        def pop_batch(self, limit, *, expand):
            assert limit == 1 and expand is False
            item = next(snapshots, None)
            return [] if item is None else [item]
    calls = []
    async def evaluate(token):
        calls.append(token)
    assert asyncio.run(loop_scheduler.evaluate_hot_queue(Queue(), evaluate,
        max_items=10, budget_s=3, clock=lambda: 1_000.)) == 2
    assert [item["price_pct_5m"] for item in calls] == [100, 500]
