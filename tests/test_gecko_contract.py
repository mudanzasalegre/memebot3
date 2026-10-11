"""Native synthetic public-v2 contracts; no provider, wallet or profitability proof."""
import asyncio
from copy import deepcopy
import math
import time

import pytest

from fetcher import geckoterminal as gt
from utils import simple_cache, price_service, rate_limiter
from utils.market_observation import fresh_market_value

MINT = "So11111111111111111111111111111111111111112"
OTHER = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAIR = "11111111111111111111111111111111"


def payload():
    return {"data":{"id":f"solana_{MINT}","type":"token","attributes":{
        "address":MINT,"symbol":"SYNTH","price_usd":"2","base_token_price_usd":"999",
        "total_reserve_in_usd":"50000","volume_usd":{"h24":"1000"},
        "market_cap_usd":None,"fdv_usd":"99999","created_at":"2026-10-01T00:00:00Z"},
        "relationships":{"top_pools":{"data":[{"id":f"solana_{PAIR}","type":"pool"}]}}},
        "included":[{"id":f"solana_{PAIR}","type":"pool","attributes":{
            "address":PAIR,"reserve_in_usd":"5000","pool_created_at":"2026-10-01T00:00:00Z",
            "price_change_percentage":{"m5":"25000"},"transactions":{"m5":{"buys":7,"sells":3}}},
            "relationships":{"base_token":{"data":{"id":f"solana_{MINT}","type":"token"}},
                "quote_token":{"data":{"id":f"solana_{OTHER}","type":"token"}}}}]}


class Response:
    def __init__(self, body, status=200):
        self.body, self.status, self.status_code = body, status, status
        self.headers, self.exited, self.closed = {}, 0, 0
    def raise_for_status(self):
        if self.status >= 400: raise RuntimeError("synthetic HTTP error")
    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.exited += 1
    async def json(self):
        if isinstance(self.body, BaseException): raise self.body
        return deepcopy(self.body)
    def close(self): self.closed += 1


class SyncResponse(Response):
    def json(self):
        if isinstance(self.body, BaseException): raise self.body
        return deepcopy(self.body)


class Client:
    def __init__(self, body, mode="async", status=200):
        self.response = (Response if mode == "async" else SyncResponse)(body, status)
        self.calls = []
    def get(self, url, **kw): self.calls.append((url,kw)); return self.response


class Limiter:
    max_calls = 10
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def acquire_sync(self): pass


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    simple_cache.cache_clear(); gt._fail_count.clear()
    monkeypatch.setattr(gt,"USE_GECKO_TERMINAL",True)
    monkeypatch.setattr(gt,"GECKO_LIMITER",Limiter())
    monkeypatch.setattr(gt,"_min_interval_s",0.)
    monkeypatch.setattr(gt,"_last_call_ts",0.)
    monkeypatch.setattr(gt,"_last_observed_ts",None,raising=False)
    monkeypatch.setattr(gt,"_cooldown_until",0.)
    def forbidden(*a,**kw): raise AssertionError("Real HTTP forbidden")
    monkeypatch.setattr(gt.requests,"get",forbidden)
    monkeypatch.setattr(gt.requests,"Session",forbidden)
    if gt.aiohttp is not None: monkeypatch.setattr(gt.aiohttp,"ClientSession",forbidden)
    yield
    simple_cache.cache_clear(); gt._fail_count.clear()


async def read(body, mode, **kwargs):
    client=Client(body,mode,kwargs.pop("status",200))
    if mode=="async": result=await gt.get_token_data_async("solana",MINT,client,force_refresh=True,**kwargs)
    else: result=gt.get_token_data("solana",MINT,client,force_refresh=True,**kwargs)
    return result,client


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["sync","async"])
async def test_original_current_public_request_and_token_pool_grain(mode):
    row,client=await read(payload(),mode)
    assert row["price_usd"]==2 and row["pair_address"]==PAIR
    assert row["liquidity_usd"]==5000 and row["liquidity_usd_is_proxy"] is False
    assert row["market_cap_usd"] is None and row["fdv_usd"]==99999
    assert fresh_market_value(row,"price_pct_5m",source="geckoterminal")==25000
    assert row["txns_last_5m"]==10 and row["txns_last_5m_buys"]==7
    assert row["created_at"] is None and row["pair_created_at"] is not None
    url,kw=client.calls[0]
    assert url==f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{MINT}"
    assert kw["params"]=={"include":"top_pools"}
    assert kw["headers"]["Accept"]=="application/json;version=20230203"
    assert kw["allow_redirects"] is False
    assert client.response.exited+client.response.closed==1


BAD_BODIES=[None,[],True,{},{"data":None},{"data":[]},{"data":True},
    {"data":{"attributes":[]}},{"data":{"attributes":True}},{"data":{"attributes":{"price_usd":"2"}}}]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["sync","async"])
@pytest.mark.parametrize("body",BAD_BODIES)
async def test_original_unknown_envelope_is_unknown_not_exception_or_identity(body,mode):
    row,client=await read(body,mode)
    assert row is None and len(client.calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["sync","async"])
@pytest.mark.parametrize("field,value",[("id",f"solana_{OTHER}"),("id",f"eth_{MINT}"),
    ("type","pool"),("attributes",None),("attributes",[])])
async def test_original_exact_token_identity_is_required(mode,field,value):
    body=payload(); body["data"][field]=value
    row,_=await read(body,mode)
    assert row is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["sync","async"])
async def test_original_attribute_address_cannot_be_relabelled(mode):
    body=payload(); body["data"]["attributes"]["address"]=OTHER
    assert (await read(body,mode))[0] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["sync","async"])
@pytest.mark.parametrize("network,address",[(None,MINT),(True,MINT),("solana",True),
    ("solana",{}),("solana",[]),("solana","0x123"),("solana/../../eth",MINT)])
async def test_original_invalid_context_does_not_start_http(mode,network,address):
    client=Client(payload(),mode)
    if mode=="async": row=await gt.get_token_data_async(network,address,client)
    else: row=gt.get_token_data(network,address,client)
    assert row is None and client.calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["sync","async"])
@pytest.mark.parametrize("value",[None,True,-1,0,float("inf"),float("nan"),{},"bad"])
async def test_original_invalid_primary_price_cannot_use_another_token_price(mode,value):
    body=payload(); body["data"]["attributes"]["price_usd"]=value
    row,_=await read(body,mode)
    assert row is not None and row["price_usd"] is None
    assert fresh_market_value(row,"price_usd") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["sync","async"])
async def test_original_zero_cap_is_not_replaced_by_fdv(mode):
    body=payload(); body["data"]["attributes"]["market_cap_usd"]=0
    row,_=await read(body,mode)
    assert row["market_cap_usd"]==0 and row["fdv_usd"]==99999


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["sync","async"])
async def test_original_token_total_reserve_is_a_proxy_without_valid_pool(mode):
    body=payload(); body["included"]=[]
    row,_=await read(body,mode)
    assert row["liquidity_usd"]==50000 and row["liquidity_usd_is_proxy"] is True
    assert row["pair_address"] is None and row["txns_last_5m"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change",["foreign_id","foreign_address","foreign_tokens","duplicate"])
async def test_original_pool_metadata_requires_identity_and_unique_link(change):
    body=payload(); p=body["included"][0]
    if change=="foreign_id": p["id"]=f"eth_{PAIR}"
    if change=="foreign_address": p["attributes"]["address"]=OTHER
    if change=="foreign_tokens": p["relationships"]["base_token"]["data"]["id"]=f"solana_{OTHER}"
    if change=="duplicate": body["included"].append(deepcopy(p))
    row,_=await read(body,"async")
    assert row["pair_address"] is None and row["liquidity_usd_is_proxy"] is True
    assert row["price_pct_5m"] is None


@pytest.mark.asyncio
async def test_original_quote_token_does_not_inherit_base_activity():
    body=payload(); rel=body["included"][0]["relationships"]
    rel["base_token"],rel["quote_token"]=rel["quote_token"],rel["base_token"]
    row,_=await read(body,"async")
    assert row["price_usd"]==2 and row["pair_address"]==PAIR
    assert row["liquidity_usd"]==5000
    assert row["price_pct_5m"] is None and row["txns_last_5m_buys"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["sync","async"])
@pytest.mark.parametrize("status",[201,204,301,302,404,429,500])
async def test_original_non_200_does_not_create_success(mode,status):
    row,_=await read(payload(),mode,status=status)
    assert row is None


@pytest.mark.asyncio
async def test_original_mixed_sync_async_throttle_uses_one_clock(monkeypatch):
    class Clock:
        mono=100.
        def time(self): return 1791687600.
        def monotonic(self): return self.mono
        def sleep(self,delay): self.mono+=delay
    clock=Clock(); waits=[]
    async def sleep(delay): waits.append(delay); clock.mono+=delay
    monkeypatch.setattr(gt,"time",clock)
    monkeypatch.setattr(gt,"_min_interval_s",2.)
    monkeypatch.setattr(gt.asyncio,"sleep",sleep)
    gt.get_token_data("solana",MINT,Client(payload(),"sync"),force_refresh=True)
    await gt.get_token_data_async("solana",MINT,Client(payload()),force_refresh=True)
    assert waits and 0<=max(waits)<=2


def test_original_public_rate_default_is_not_an_entitlement_to_30_calls():
    assert 1<=rate_limiter.GECKO_LIMITER.max_calls<=10


@pytest.mark.asyncio
async def test_original_waiters_retry_permission_after_shared_window_wakeup(monkeypatch):
    class Clock:
        now=100.
        def monotonic(self): return self.now
    clock=Clock(); sleeps=[]
    async def sleep(delay):
        future=asyncio.get_running_loop().create_future(); sleeps.append((delay,future)); await future
    monkeypatch.setattr(rate_limiter,"time",clock)
    monkeypatch.setattr(rate_limiter.asyncio,"sleep",sleep)
    limiter=rate_limiter.RateLimiter(1,60.)
    await limiter.acquire()
    tasks=[asyncio.create_task(limiter.acquire()) for _ in range(2)]
    try:
        for _ in range(10): await asyncio.tasks.sleep(0)
        assert len(sleeps)==2
        clock.now=160.
        for _,f in sleeps[:2]: f.set_result(None)
        for _ in range(10): await asyncio.tasks.sleep(0)
        assert sum(t.done() for t in tasks)==1
        assert len(sleeps)==3
        clock.now=220.; sleeps[-1][1].set_result(None)
        await asyncio.gather(*tasks)
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


class BlockingResponse(Response):
    def __init__(self,body):
        super().__init__(body); self.started=asyncio.Event(); self.release=asyncio.Event()
    async def json(self):
        self.started.set(); await self.release.wait(); return await super().json()


@pytest.mark.asyncio
async def test_native_normal_followers_share_original_receipt_and_detached_results():
    client=Client(payload()); client.response=BlockingResponse(payload())
    tasks=[asyncio.create_task(gt.get_token_data_async("solana",MINT,client)) for _ in range(8)]
    await client.response.started.wait()
    for _ in range(12): await asyncio.tasks.sleep(0)
    assert len(client.calls)==1 and gt.request_runtime_snapshot()["requests"]["joiners"]==7
    tasks[1].cancel()
    with pytest.raises(asyncio.CancelledError): await tasks[1]
    client.response.release.set()
    rows=await asyncio.gather(*(t for i,t in enumerate(tasks) if i!=1))
    assert all(r["price_pct_5m"]==25000 for r in rows)
    proof=deepcopy(rows[1]["market_observation"])
    rows[0]["liquidity"]["usd"]=999; rows[0]["market_observation"]["fields"].clear()
    cached=await gt.get_token_data_async("solana",MINT,client)
    assert cached["market_observation"]==proof and cached["liquidity_usd"]==5000
    assert len(client.calls)==1 and gt.request_runtime_snapshot()["active_requests"]==0


@pytest.mark.asyncio
@pytest.mark.parametrize("old_failed",[False,True])
async def test_native_late_old_response_cannot_overwrite_forced_generation(old_failed):
    old_body=payload(); old_body["data"]["attributes"]["price_usd"]="1"
    old=Client(old_body); old.response=BlockingResponse(None if old_failed else old_body)
    task=asyncio.create_task(gt.get_token_data_async("solana",MINT,old))
    await old.response.started.wait()
    new=Client(payload())
    fresh=await gt.get_token_data_async("solana",MINT,new,force_refresh=True)
    old.response.release.set(); older=await task
    assert older is None if old_failed else older["price_usd"]==1
    cached=await gt.get_token_data_async("solana",MINT,new)
    assert cached["price_usd"]==2 and cached["market_observation"]==fresh["market_observation"]
    assert len(new.calls)==1 and gt._fail_count.get(f"gt:{gt._CONTRACT}:solana:{MINT}",0)==0


@pytest.mark.asyncio
async def test_native_repeated_cancel_drains_owned_session_before_slot_release(monkeypatch):
    class Owned(Client):
        def __init__(self):
            super().__init__(payload()); self.response=BlockingResponse(payload())
            self.closing=asyncio.Event(); self.released=asyncio.Event(); self.closed=0
        async def close(self):
            self.closing.set(); await self.released.wait(); self.closed+=1
    client=Owned(); monkeypatch.setattr(gt.aiohttp,"ClientSession",lambda:client)
    task=asyncio.create_task(gt.get_token_data_async("solana",MINT))
    await client.response.started.wait(); task.cancel(); await client.closing.wait()
    task.cancel(); await asyncio.tasks.sleep(0)
    assert not task.done() and gt.request_runtime_snapshot()["active_requests"]==1
    assert gt.request_runtime_snapshot()["requests"]["owners"]==1
    client.released.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert client.closed==1 and gt.request_runtime_snapshot()["active_requests"]==0
    assert gt.request_runtime_snapshot()["requests"]["loop_buckets"]==0
    assert gt._fail_count.get(f"gt:{gt._CONTRACT}:solana:{MINT}",0)==0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["sync","async"])
async def test_native_shared_capacity_rejection_does_not_poison_provider_cache(mode):
    leases=[(f"lease-{i}",gt._begin_request(f"lease-{i}")) for i in range(16)]
    try:
        row,client=await read(payload(),mode)
        assert row is None and client.calls==[]
        assert gt._fail_count.get(f"gt:{gt._CONTRACT}:solana:{MINT}",0)==0
        assert simple_cache.cache_get(f"gt:{gt._CONTRACT}:solana:{MINT}") is None
    finally:
        for key,generation in leases: gt._end_request(key,generation)


@pytest.mark.asyncio
async def test_native_common_fallback_keeps_uncapped_selected_pool_signal(monkeypatch):
    class Owned(Client):
        async def close(self): pass
    client=Owned(payload()); monkeypatch.setattr(gt.aiohttp,"ClientSession",lambda:client)
    monkeypatch.setattr(price_service,"_USE_BIRDEYE",False)
    monkeypatch.setattr(price_service,"_USE_JUPITER_PRICE",False)
    monkeypatch.setattr(price_service,"_USE_JUPITER_IMPACT",False)
    monkeypatch.setattr(price_service,"USE_GECKO_TERMINAL",True)
    monkeypatch.setattr(price_service,"get_gt_data_async",gt.get_token_data_async)
    async def absent(*a,**k): return None
    monkeypatch.setattr(price_service.dexscreener,"get_pair",absent)
    row=await price_service._query_sources(MINT,use_gt=True,fields_needed=("price_usd","liquidity_usd"),force_refresh=True)
    assert row["price_usd"]==2 and row["liquidity_usd"]==5000
    assert row["price_pct_5m"]==25000 and row["created_at"] is None
    assert fresh_market_value(row,"price_pct_5m",source="geckoterminal")==25000
    assert row["liquidity_usd_is_proxy"] is False and len(client.calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize("value",[None,True,0,-1,float("inf"),float("nan"),"bad"])
async def test_native_invalid_timeout_has_no_http(value):
    row,client=await read(payload(),"async",timeout=value)
    assert row is None and client.calls==[]


def test_native_sync_cleanup_exception_is_unknown_not_uncaught():
    client=Client(payload(),"sync")
    def fail(): raise RuntimeError("synthetic close error")
    client.response.close=fail
    assert gt.get_token_data("solana",MINT,client,force_refresh=True) is None
    assert gt.request_runtime_snapshot()["active_requests"]==0


def test_native_limiter_and_ownership_allow_successive_event_loops(monkeypatch):
    monkeypatch.setattr(gt,"GECKO_LIMITER",rate_limiter.RateLimiter(10))
    for _ in range(2):
        row=asyncio.run(gt.get_token_data_async("solana",MINT,Client(payload()),force_refresh=True))
        assert row["price_usd"]==2
    assert gt.request_runtime_snapshot()["requests"]["loop_buckets"]==0


@pytest.mark.asyncio
@pytest.mark.parametrize("value",[None,True,-1,float("inf"),float("nan"),{},"bad"])
async def test_native_invalid_selected_pool_reserve_is_not_replaced_by_token_total(value):
    body=payload(); body["included"][0]["attributes"]["reserve_in_usd"]=value
    row,_=await read(body,"async")
    assert row["price_usd"]==2 and row["liquidity_usd"] is None
    assert fresh_market_value(row,"liquidity_usd") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value",[("transactions",[]),("transactions",True),
    ("transactions",{"m5":True}),("transactions",{"m5":{"buys":True,"sells":3}}),
    ("transactions",{"m5":{"buys":1.5,"sells":3}}),
    ("price_change_percentage",[]),("price_change_percentage",True)])
async def test_native_optional_pool_shapes_are_isolated(field,value):
    body=payload(); body["included"][0]["attributes"][field]=value
    row,_=await read(body,"async")
    assert row["price_usd"]==2 and row["liquidity_usd"]==5000
    if field=="transactions": assert row["txns_last_5m"] is None
    else: assert row["price_pct_5m"] is None


@pytest.mark.asyncio
async def test_native_zero_selected_pool_reserve_stays_measured_zero():
    body=payload(); body["included"][0]["attributes"]["reserve_in_usd"]=0
    row,_=await read(body,"async")
    assert fresh_market_value(row,"liquidity_usd")==0
    assert row["liquidity_usd_is_proxy"] is False


@pytest.mark.asyncio
async def test_native_owned_close_does_not_restamp_original_http_receipt(monkeypatch):
    start=time.time()
    class Clock:
        now=start
        def time(self): return self.now
        def monotonic(self): return time.monotonic()
    clock=Clock()
    class Owned(Client):
        async def close(self): clock.now+=20.
    client=Owned(payload())
    monkeypatch.setattr(gt,"time",clock)
    monkeypatch.setattr(gt.aiohttp,"ClientSession",lambda:client)
    row=await gt.get_token_data_async("solana",MINT)
    assert row["market_observation"]["fields"]["price_usd"]["received_at"]==start
    later=await gt.get_token_data_async("solana",MINT)
    assert later["market_observation"]==row["market_observation"] and len(client.calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize("key",[f"gt:solana:{MINT}",f"gt:gecko_public_token_pool_v1:solana:{MINT}"])
async def test_native_untyped_or_wrong_identity_cache_cannot_supply_token(key):
    simple_cache.cache_set(key,{"address":OTHER,"price_usd":999,"gecko_contract_version":gt._CONTRACT},ttl=600)
    client=Client(payload())
    row=await gt.get_token_data_async("solana",MINT,client)
    assert row["address"]==MINT and row["price_usd"]==2 and len(client.calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize("url",["http://api.geckoterminal.com/api/v2",
    "https://other.invalid/api/v2","https://api.geckoterminal.com/api/v2?key=synthetic",
    "https://[invalid/api/v2", "https://[invalid]extra/api/v2"])
async def test_native_nonpublic_gateway_is_not_a_silent_protocol_substitute(monkeypatch,url):
    monkeypatch.setattr(gt,"_BASE_URL",url)
    row,client=await read(payload(),"async")
    assert row is None and client.calls==[]


@pytest.mark.asyncio
async def test_native_owned_async_close_failure_is_unknown_and_releases_admission(monkeypatch):
    class Owned(Client):
        async def close(self): raise RuntimeError("synthetic close failure")
    client=Owned(payload())
    before=gt.request_runtime_snapshot()["diagnostic_outcomes"]
    monkeypatch.setattr(gt.aiohttp,"ClientSession",lambda:client)
    row=await gt.get_token_data_async("solana",MINT)
    after=gt.request_runtime_snapshot()
    assert row is None and after["active_requests"]==0 and after["latest_generations"]==0
    assert after["diagnostic_outcomes"]["cleanup_failed"]==before["cleanup_failed"]+1
    assert after["diagnostic_outcomes"]["success"]==before["success"]+1
    assert after["diagnostic_outcomes"]["request_error"]==before["request_error"]+1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["sync","async"])
async def test_native_diagnostics_are_aggregate_events_not_unique_wire_billing(mode):
    before=gt.request_runtime_snapshot()["diagnostic_outcomes"]
    await read(payload(),mode)
    await read(payload(),mode,status=429)
    after=gt.request_runtime_snapshot()["diagnostic_outcomes"]
    assert after["success"]==before["success"]+1
    assert after["rate_limited"]==before["rate_limited"]+1
    assert after["http_unavailable"]==before["http_unavailable"]+1


def test_native_snapshot_contains_only_aggregate_pressure():
    import json
    state=gt.request_runtime_snapshot()
    assert state["active_requests"]==0 and state["latest_generations"]==0
    assert MINT not in json.dumps(state) and "attributes" not in state
    assert set(state["diagnostic_outcomes"])=={"success","invalid_resource","http_unavailable",
        "request_error","rate_limited","cancelled","capacity_rejected","cleanup_failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["sync","async"])
async def test_native_legacy_numeric_aliases_keep_their_own_token_grain(mode):
    row,_=await read(payload(),mode)
    assert row["total_reserve_in_usd"]==50000. and isinstance(row["total_reserve_in_usd"],float)
    assert row["liquidity_usd"]==5000. and row["liquidity_usd_is_proxy"] is False
    assert row["volume_usd_24h"]==row["volume_24h_usd"]==1000.
    assert row["market_cap_usd"] is None and row["fdv_usd"]==99999.
