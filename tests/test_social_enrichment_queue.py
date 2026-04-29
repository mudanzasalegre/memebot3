from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import runtime.social_enrichment_queue as queue_mod
from analytics.social_signal import social_signal_from_profile


@pytest.mark.asyncio
async def test_social_queue_schedules_without_blocking_hot_path(monkeypatch) -> None:
    events: list[tuple[str, str]] = []

    async def fake_fetch(address: str):
        return social_signal_from_profile({"links": {"twitterUrl": f"https://x.com/{address}"}}, latency_ms=5)

    monkeypatch.setattr(
        queue_mod,
        "CFG",
        SimpleNamespace(
            SOCIALS_ENABLED=True,
            SOCIALS_CACHE_TTL_S=600,
            SOCIALS_MAX_CONCURRENT=2,
            SOCIALS_ASYNC_ONLY=True,
            SOCIALS_HOT_PATH_BLOCKING=False,
        ),
    )
    monkeypatch.setattr(queue_mod, "fetch_social_profile", fake_fetch)
    monkeypatch.setattr(queue_mod, "record_social_links", lambda *args, **kwargs: None)
    monkeypatch.setattr(queue_mod, "record_social_signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(queue_mod, "record_runtime_event", lambda event, address, **payload: events.append((event, address)))

    q = queue_mod.SocialEnrichmentQueue()
    scheduled = q.schedule({"address": "abc", "symbol": "ABC"}, lane="pump_early_green_candle_sniper")

    assert scheduled is True
    assert ("social_enrichment_scheduled", "abc") in events
    for _ in range(10):
        if q.get_cached("abc") is not None:
            break
        await asyncio.sleep(0.01)
    assert q.get_cached("abc") is not None
    assert ("social_enrichment_completed", "abc") in events


def test_social_queue_does_not_schedule_without_loop(monkeypatch) -> None:
    monkeypatch.setattr(
        queue_mod,
        "CFG",
        SimpleNamespace(SOCIALS_ENABLED=True, SOCIALS_CACHE_TTL_S=600, SOCIALS_MAX_CONCURRENT=2),
    )
    q = queue_mod.SocialEnrichmentQueue()

    assert q.schedule({"address": "abc"}, lane="green") is False
