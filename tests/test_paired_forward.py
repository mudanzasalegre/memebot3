"""Shared secondary workload with synthetic providers; no bot start or fills."""
import asyncio
import datetime as dt
from dataclasses import replace

import pytest

from config.config import CFG
from research_loop import paired_forward as paired, forward_budget as store

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)


def cfg(**changes):
    return replace(CFG, DRY_RUN=True, PAPER_ENTRY_RESEARCH_ENABLED=True,
                   PAPER_RUNNER_RESEARCH_ENABLED=True, **changes)


def test_one_shared_price_union_and_one_extra_quote_per_minute(tmp_path, monkeypatch):
    calls, observations = [], []
    monkeypatch.setattr(paired.runner_forward, "active_tokens", lambda _: {"a", "b"})
    monkeypatch.setattr(paired.entry_gate_forward, "active_tokens", lambda _: {"b", "c"})
    async def prices(tokens):
        calls.append(tokens)
        return {key: 2. for key in tokens}
    def collector(owner, requested):
        async def tick(**kwargs):
            observations.append(await kwargs["prices_func"](requested))
            claimed = store.claim(tmp_path, owner, now=kwargs["now"])
            return {"quote_calls": int(claimed)}
        return tick
    monkeypatch.setattr(paired.runner_forward, "tick", collector("runner_exit", ["a", "b"]))
    monkeypatch.setattr(paired.entry_gate_forward, "tick", collector("entry_gate", ["b", "c"]))
    result = asyncio.run(paired.tick(root=tmp_path, cfg=cfg(), now=T0, prices_func=prices))
    assert calls == [["a", "b", "c"]]
    assert observations == [{"a": 2., "b": 2.}, {"b": 2., "c": 2.}]
    assert result["extra_quote_calls"] == 1
    assert asyncio.run(paired.tick(root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(seconds=59),
                                  prices_func=prices))["status"] == "throttled"
    assert len(calls) == 1


@pytest.mark.parametrize("payload", ["broken", "{}", '{"last_tick_at":"invalid"}'])
def test_corrupt_shared_market_clock_cannot_reset_budget(tmp_path, payload):
    path = tmp_path / "data/research/paired_forward_market_clock.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload)
    result = asyncio.run(paired.tick(root=tmp_path, cfg=cfg(), now=T0))
    assert result["status"] == "corrupt_clock"
    assert path.read_text() == payload


def test_live_disables_both_collectors_without_io(tmp_path):
    assert asyncio.run(paired.tick(root=tmp_path, cfg=replace(cfg(), DRY_RUN=False), now=T0))["status"] == "disabled"
    assert not list(tmp_path.rglob("*"))


def test_failed_secondary_does_not_block_other_component(tmp_path, monkeypatch):
    async def broken(**kwargs): raise ValueError("synthetic corrupt journal")
    async def healthy(**kwargs): return {"status": "idle", "quote_calls": 0}
    monkeypatch.setattr(paired.runner_forward, "active_tokens", lambda _: set())
    monkeypatch.setattr(paired.entry_gate_forward, "active_tokens", lambda _: set())
    monkeypatch.setattr(paired.runner_forward, "tick", broken)
    monkeypatch.setattr(paired.entry_gate_forward, "tick", healthy)
    result = asyncio.run(paired.tick(root=tmp_path, cfg=cfg(), now=T0))
    assert result["runner"]["status"] == "secondary_unavailable"
    assert result["entry"]["status"] == "idle"


def test_shared_capacity_is_not_silently_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(paired.runner_forward, "active_tokens", lambda _: {f"token-{i}" for i in range(129)})
    monkeypatch.setattr(paired.entry_gate_forward, "active_tokens", lambda _: set())
    async def forbidden(tokens): pytest.fail("over-capacity cohort must not query a truncated population")
    async def collector(**kwargs):
        assert await kwargs["prices_func"](["token-0"]) == {}
        return {"quote_calls": 0}
    monkeypatch.setattr(paired.runner_forward, "tick", collector)
    monkeypatch.setattr(paired.entry_gate_forward, "tick", collector)
    result = asyncio.run(paired.tick(root=tmp_path, cfg=cfg(), now=T0, prices_func=forbidden))
    assert result["status"] == "shared_capacity_exceeded"
    assert result["market_tokens"] == 129
