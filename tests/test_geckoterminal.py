"""Legacy numeric compatibility and complete monotonic throttle intervals.

Pure native fixtures: valid typed Solana token, owned fake Session/Response,
no real HTTP. Forced calls exercise throttle; cached calls do not consume it.
"""
from copy import deepcopy
import time
import types

import pytest

from fetcher import geckoterminal as gt
from utils.simple_cache import cache_clear

MINT = "So11111111111111111111111111111111111111112"
_MOCK_JSON = {"data":{"id":f"solana_{MINT}", "type":"token", "attributes":{
    "address":MINT, "price_usd":"0.01234", "market_cap_usd":None,
    "fdv_usd":"123456", "total_reserve_in_usd":"9876.5",
    "volume_usd":{"h24":"6543.21"}}}}


class MockResponse:
    status_code = 200
    headers = {}
    def json(self): return deepcopy(_MOCK_JSON)
    def close(self): pass


@pytest.fixture
def mock_requests_get(monkeypatch):
    class Client:
        def __init__(self): self.calls = []; self.closed = 0
        def get(self,url,**kwargs):
            assert url == f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{MINT}"
            assert kwargs["allow_redirects"] is False
            self.calls.append((url,kwargs))
            return MockResponse()
        def close(self): self.closed += 1
    client = Client()
    def forbidden(*args,**kwargs): raise AssertionError("Real HTTP forbidden")
    cache_clear()
    monkeypatch.setattr(gt,"USE_GECKO_TERMINAL",True)
    monkeypatch.setattr(gt.requests,"get",forbidden)
    monkeypatch.setattr(gt.requests,"Session",lambda:client)
    monkeypatch.setattr(gt,"GECKO_LIMITER",types.SimpleNamespace(max_calls=10,acquire_sync=lambda:None))
    monkeypatch.setattr(gt,"_cooldown_until",0.)
    monkeypatch.setattr(gt,"_last_observed_ts",None)
    yield client
    cache_clear()


@pytest.fixture
def patch_time(monkeypatch):
    counter = {"t":10000.}
    sleeps = []
    def fake_monotonic():
        counter["t"] += .1
        return counter["t"]
    def fake_sleep(seconds):
        sleeps.append(seconds)
        counter["t"] += seconds
    # Patch only this adapter's clock, not shared cache or other modules.
    monkeypatch.setattr(gt,"time",types.SimpleNamespace(
        monotonic=fake_monotonic,time=time.time,sleep=fake_sleep))
    return sleeps


def test_get_token_data_numeric_conversion(mock_requests_get,patch_time,monkeypatch):
    monkeypatch.setattr(gt,"_min_interval_s",2.)
    monkeypatch.setattr(gt,"_last_call_ts",10000.)
    out1=gt.get_token_data("solana",MINT,force_refresh=True)
    out2=gt.get_token_data("solana",MINT,force_refresh=True)
    for row in (out1,out2):
        for field,expected in (("price_usd",.01234),("fdv_usd",123456.),
            ("total_reserve_in_usd",9876.5),("volume_usd_24h",6543.21)):
            assert isinstance(row[field],float)
            assert row[field] == pytest.approx(expected)
        assert row["market_cap_usd"] is None
        assert row["liquidity_usd_is_proxy"] is True
    assert max(patch_time) == pytest.approx(1.8)
    assert len(mock_requests_get.calls)==2 and mock_requests_get.closed==2
    before=list(patch_time)
    cached=gt.get_token_data("solana",MINT)
    assert cached["market_observation"]==out2["market_observation"]
    assert len(mock_requests_get.calls)==2 and patch_time==before


def test_sync_throttle_respects_complete_interval(monkeypatch):
    times=iter([1.,2.,2.1,4.])
    sleeps=[]
    monkeypatch.setattr(gt,"_min_interval_s",2.)
    monkeypatch.setattr(gt,"_last_call_ts",0.)
    monkeypatch.setattr(gt,"_last_observed_ts",None)
    monkeypatch.setattr(gt,"time",types.SimpleNamespace(
        monotonic=lambda:next(times),sleep=sleeps.append))
    gt._throttle_internal()
    gt._throttle_internal()
    assert sleeps==pytest.approx([1.,1.9])
    assert gt._last_call_ts==4.
