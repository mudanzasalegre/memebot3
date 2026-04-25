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
