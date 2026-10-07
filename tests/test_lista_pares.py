from __future__ import annotations

from utils import lista_pares


def _reset_queue_state() -> None:
    lista_pares._pair_watch.clear()
    lista_pares._processed.clear()


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
