# memebot3/tests/test_geckoterminal.py
"""
Tests del wrapper GeckoTerminal.

• Comprueba que `get_token_data` convierte los valores string → numérico.
• Verifica que el rate-limit obliga a dormir (~≥1.9 s) entre llamadas
  consecutivas (parcheando `time.time` y `time.sleep`).

Se usan parches (monkeypatch) para:
  – `requests.get`             → MockResponse
  – `time.time` y `time.sleep` → control de la ventana y captura de sleeps
"""

from __future__ import annotations

import types
from typing import Any, Dict, List

import pytest

# --- mock JSON devuelto por GeckoTerminal ---
_MOCK_JSON: Dict[str, Any] = {
    "data": {
        "attributes": {
            "price_usd": "0.01234",
            "fdv_usd": "123456",
            "total_reserve_in_usd": "9876.5",
            "volume_usd": {"h24": "6543.21"},
        }
    }
}


# -----------------------------------------------------------------
class MockResponse:
    status_code = 200

    def json(self) -> Dict[str, Any]:
        return _MOCK_JSON

    def raise_for_status(self):  # mimics requests.Response API
        pass


# -----------------------------------------------------------------
@pytest.fixture
def mock_requests_get(monkeypatch):
    """Parcha requests.get para que devuelva siempre MockResponse."""
    import requests

    def _fake_get(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr(requests, "get", _fake_get)


@pytest.fixture
def patch_time(monkeypatch):
    """
    Parchea time.time y time.sleep:
      • time.time avanza 0.1 s cada llamada sucesiva
      • time.sleep registra los segundos “dormidos” sin esperar realmente
    Devuelve la lista `sleeps` para inspección en las pruebas.
    """
    import time

    counter = {"t": 0.0}
    sleeps: List[float] = []

    def fake_time() -> float:
        counter["t"] += 0.1
        return counter["t"]

    def fake_sleep(sec: float):
        sleeps.append(sec)
        # NO dormimos en realidad

    monkeypatch.setattr(time, "time", fake_time, raising=True)
    monkeypatch.setattr(time, "sleep", fake_sleep, raising=True)
    # Exponemos la lista para el test
    return sleeps


# -----------------------------------------------------------------
def test_get_token_data_numeric_conversion(mock_requests_get, patch_time, monkeypatch):
    """
    • price_usd, fdv_usd, total_reserve_in_usd, volume_usd_24h deben ser numéricos.
    • Se ejecutan dos llamadas seguidas para disparar el rate-limit; se
      comprueba la pausa descontando el tiempo transcurrido entre llamadas.
    """
    # importamos late para que los parches ya estén aplicados
    from fetcher.geckoterminal import get_token_data
    from fetcher import geckoterminal
    monkeypatch.setattr(geckoterminal, "_min_interval_s", 2.0)
    monkeypatch.setattr(geckoterminal, "_last_call_ts", 0.0)

    out1 = get_token_data("solana", "0xABCDEF1234567890")
    out2 = get_token_data("solana", "0xABCDEF1234567890")  # misma addr: da igual al test

    # --- conversión numérica ---
    assert isinstance(out1["price_usd"], float)
    assert isinstance(out1["fdv_usd"], float)
    assert isinstance(out1["total_reserve_in_usd"], float)
    assert isinstance(out1["volume_usd_24h"], float)

    # Los valores deben coincidir (numéricamente) con el mock
    assert out1["price_usd"] == pytest.approx(0.01234)
    assert out1["fdv_usd"] == pytest.approx(123456.0)
    assert out1["total_reserve_in_usd"] == pytest.approx(9876.5)
    assert out1["volume_usd_24h"] == pytest.approx(6543.21)

    # --- rate-limit: al menos un sleep ~2 s ---
    sleeps = patch_time        # alias claro
    assert sleeps, "El rate-limit no llamó a time.sleep()"
    # Tomamos la mayor pausa registrada
    # The fake clock advances 0.1s per read; elapsed time counts toward the
    # interval too. A sleep alone need not be the complete 2s interval.
    assert max(sleeps) == pytest.approx(1.8)


def test_sync_throttle_respects_complete_interval(monkeypatch):
    from fetcher import geckoterminal
    times = iter([1.0, 2.0, 2.1, 4.0])
    sleeps = []
    monkeypatch.setattr(geckoterminal, "_min_interval_s", 2.0)
    monkeypatch.setattr(geckoterminal, "_last_call_ts", 0.0)
    monkeypatch.setattr(geckoterminal.time, "time", lambda: next(times))
    monkeypatch.setattr(geckoterminal.time, "sleep", sleeps.append)
    geckoterminal._throttle_internal()
    geckoterminal._throttle_internal()
    assert sleeps == pytest.approx([1.0, 1.9])
    assert geckoterminal._last_call_ts == 4.0
