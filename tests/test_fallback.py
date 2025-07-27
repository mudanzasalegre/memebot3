# memebot3/tests/test_fallback.py
import pathlib
import sys
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import analytics.trend as trend

class FakeResp:
    def __init__(self, status, json_data=None):
        self.status = status
        self._json = json_data or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def json(self):
        return self._json

class FakeSession:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    def get(self, url):
        return self._resp


def make_session(resp):
    def _factory(*args, **kwargs):
        return FakeSession(resp)
    return _factory

@pytest.mark.asyncio
async def test_trend_signal_fallback(monkeypatch):
    async def fake_get_pair(addr):
        return {"address": addr, "price_pct_5m": 10}

    monkeypatch.setattr("fetcher.dexscreener.get_pair", fake_get_pair)
    monkeypatch.setattr(trend.aiohttp, "ClientSession", make_session(FakeResp(404)))

    sig, used = await trend.trend_signal("ADDR")
    assert sig in {"up", "down", "flat"}
    assert used is True