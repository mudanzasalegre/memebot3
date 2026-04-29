from __future__ import annotations

import runtime.hot_queue as hot_queue
from runtime.hot_queue import HotQueue


def test_hot_queue_dedupes_and_prioritizes() -> None:
    q = HotQueue(max_size=10, max_age_min=20, dedup_ttl_s=1800)
    token = {"address": "A", "discovered_via": "pumpfun", "price_pct_5m": 80, "txns_last_5m": 100}
    assert q.add(token, source="pumpfun") is True
    assert q.add(token, source="pumpfun") is False
    batch = q.pop_batch(1)
    assert batch[0]["address"] == "A"
    assert q.snapshot()["size"] == 0


def test_hot_queue_can_persist_runtime_events(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(
        hot_queue,
        "record_runtime_event",
        lambda event_type, address, **payload: events.append((event_type, address, payload)),
    )
    q = HotQueue(max_size=10, max_age_min=20, dedup_ttl_s=1800, persist_events=True)

    q.add({"address": "A", "discovered_via": "pumpfun", "price_pct_5m": 80}, source="pumpfun")
    q.pop_batch(1)

    assert [event[0] for event in events] == ["hot_queue_add", "hot_queue_eval"]
    assert events[0][1] == "A"
