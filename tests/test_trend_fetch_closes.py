# memebot3/tests/test_trend_fetch_closes.py
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import analytics.trend as trend
from fetcher import dexscreener


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
    def __init__(self, responses):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    def get(self, url):
        return self._responses.pop(0)


def make_session(responses):
    def _factory(*args, **kwargs):
        return FakeSession(responses)

    return _factory


@pytest.mark.asyncio
async def test_fetch_closes_pair_chart_404(monkeypatch):
    async def fake_get_pair(addr):
        return {"address": "PAIR"}

    monkeypatch.setattr(dexscreener, "get_pair", fake_get_pair)
    responses = [FakeResp(404)]
    monkeypatch.setattr(trend.aiohttp, "ClientSession", make_session(responses))

    closes = await trend._fetch_closes("ADDR")
    assert closes == []


@pytest.mark.asyncio
async def test_fetch_closes_raises_without_pair(monkeypatch):
    async def fake_get_pair(addr):
        return None

    monkeypatch.setattr(dexscreener, "get_pair", fake_get_pair)
    responses = [FakeResp(404)]
    monkeypatch.setattr(trend.aiohttp, "ClientSession", make_session(responses))

    with pytest.raises(trend.Trend404Retry):
        await trend._fetch_closes("ADDR")