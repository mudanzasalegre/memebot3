from __future__ import annotations

from utils import lista_pares
from runtime import loop_scheduler
import asyncio


def _reset_queue_state() -> None:
    lista_pares._pair_watch.clear()
    lista_pares._processed.clear()


def test_incremental_legacy_service_rotates_without_spending_retries_or_dropping_tail(monkeypatch):
    _reset_queue_state()
    now = [1_000.]
    monkeypatch.setattr(lista_pares.time, "time", lambda: now[0])
    monkeypatch.setattr(lista_pares, "MAX_INCOMPLETE_SEC", 600)
    for address in ("A", "B", "C"):
        lista_pares._pair_watch[address] = {"retries": 3, "first_seen": now[0], "next_try": now[0], "attempts": 0}
    calls = []
    async def evaluate(address):
        calls.append(address)
        now[0] += 20
    for _ in range(3):
        assert asyncio.run(loop_scheduler.evaluate_ready_queue(lista_pares.next_ready_pair, evaluate,
            max_items=10, budget_s=3, clock=lambda: now[0])) == 1
    assert calls == ["A", "B", "C"] and len(lista_pares._pair_watch) == 3
    assert all(meta["retries"] == 3 and meta["attempts"] == 0 for meta in lista_pares._pair_watch.values())


def test_fast_legacy_cycle_evaluates_each_ready_address_only_once(monkeypatch):
    _reset_queue_state()
    monkeypatch.setattr(lista_pares.time, "time", lambda: 1_000.)
    monkeypatch.setattr(lista_pares, "MAX_INCOMPLETE_SEC", 600)
    for address in ("A", "B", "C"):
        lista_pares._pair_watch[address] = {"retries": 3, "first_seen": 1_000., "next_try": 1_000., "attempts": 0}
    calls = []
    async def evaluate(address):
        calls.append(address)
    assert asyncio.run(loop_scheduler.evaluate_ready_queue(lista_pares.next_ready_pair,
        evaluate, max_items=10, budget_s=3, clock=lambda: 1_000.)) == 3
    assert calls == ["A", "B", "C"]
    assert set(lista_pares._pair_watch) == {"A", "B", "C"}
    assert all(meta["retries"] == 3 and meta["attempts"] == 0 for meta in lista_pares._pair_watch.values())


def test_incremental_service_preserves_configured_limits_above_one_hundred():
    pending = iter(range(150))
    calls = []
    async def evaluate(item):
        calls.append(item)
    assert asyncio.run(loop_scheduler.evaluate_ready_queue(lambda: next(pending, None),
        evaluate, max_items=150, budget_s=3, clock=lambda: 1_000.)) == 150
    assert calls == list(range(150))


def test_temporary_strategy_requeues_preserve_retry_budget(monkeypatch) -> None:
    _reset_queue_state()
    monkeypatch.setattr(lista_pares, "NON_DECREMENT_REASON_PREFIXES", ("strategy:confirm_snapshots", "live_profit_gate:"))
    monkeypatch.setattr(lista_pares, "log_queue_add", lambda *args, **kwargs: None)
    monkeypatch.setattr(lista_pares, "log_queue_requeue", lambda *args, **kwargs: None)
    monkeypatch.setattr(lista_pares, "log_queue_drop", lambda *args, **kwargs: None)

    addr = "test-preserve-budget"
    assert lista_pares.agregar_si_nuevo(addr, retries=2) is True

    assert lista_pares.requeue(addr, reason="strategy:confirm_snapshots", backoff=1) is True
    assert lista_pares.retries_left(addr) == 2
    assert int((lista_pares.meta(addr) or {}).get("attempts") or 0) == 1

    assert lista_pares.requeue(addr, reason="live_profit_gate:liq<10000", backoff=1) is True
    assert lista_pares.retries_left(addr) == 2
    assert int((lista_pares.meta(addr) or {}).get("attempts") or 0) == 2

    assert lista_pares.requeue(addr, reason="no_liq", backoff=1) is True
    assert lista_pares.retries_left(addr) == 1


def test_obtener_pares_drops_expired_items_before_returning(monkeypatch) -> None:
    _reset_queue_state()
    now = 1_000.0
    drops: list[tuple[str, dict[str, object]]] = []
    persisted: list[str] = []
    monkeypatch.setattr(lista_pares.time, "time", lambda: now)
    monkeypatch.setattr(lista_pares, "MAX_INCOMPLETE_SEC", 600)
    monkeypatch.setattr(
        lista_pares,
        "log_queue_drop",
        lambda addr, **payload: drops.append((addr, payload)),
    )
    monkeypatch.setattr(lista_pares, "_persist", persisted.append)

    lista_pares._pair_watch.update(
        {
            "expired-ready": {
                "retries": 3,
                "first_seen": now - 601,
                "next_try": now - 1,
                "attempts": 2,
                "reason": "dex_nil",
            },
            "expired-cooldown": {
                "retries": 4,
                "first_seen": now - 601,
                "next_try": now + 30,
                "attempts": 1,
                "reason": "dex_nil",
            },
            "ready": {
                "retries": 5,
                "first_seen": now - 600,
                "next_try": now,
                "attempts": 0,
                "reason": "",
            },
            "cooldown": {
                "retries": 5,
                "first_seen": now - 20,
                "next_try": now + 1,
                "attempts": 0,
                "reason": "",
            },
        }
    )

    assert lista_pares.obtener_pares() == ["ready"]
    assert set(lista_pares._pair_watch) == {"ready", "cooldown"}
    assert lista_pares._processed == {"expired-ready", "expired-cooldown"}
    assert persisted == ["expired-ready", "expired-cooldown"]
    assert drops == [
        (
            "expired-ready",
            {
                "reason": "incomplete_timeout",
                "attempts": 2,
                "retries_left": 3,
                "first_seen_epoch_s": now - 601,
            },
        ),
        (
            "expired-cooldown",
            {
                "reason": "incomplete_timeout",
                "attempts": 1,
                "retries_left": 4,
                "first_seen_epoch_s": now - 601,
            },
        ),
    ]
