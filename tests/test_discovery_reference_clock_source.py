"""Original parser receipt clock survives UTC/numeric clock granularity gaps."""
from copy import deepcopy
import datetime as dt
from types import SimpleNamespace
import time
from unittest.mock import AsyncMock

import pytest

from analytics import mint_creation_reference as reference
from analytics import token_birth as birth
from analytics.token_time import compute_age_minutes
from fetcher import pumpfun
from runtime import entry_observation,token_birth_enrichment as route
from runtime import hot_queue as native_queue
from runtime.hot_queue import HotQueue
from runtime.rpc_token_birth import RpcTokenBirthResolver
from test_entry_observation import observed,preparation_namespace
from test_rpc_birth_enrichment import candidate,receipt
from utils.solana_execution import endpoint_fingerprint


def clocks(monkeypatch,offset):
    wall=[float(int(time.time())+60)]
    utc=dt.datetime.fromtimestamp(wall[0]+offset,dt.timezone.utc)
    calls=[]
    def numeric():
        calls.append('numeric')
        return wall[0]
    def observed_utc():
        calls.append('utc')
        return utc
    monkeypatch.setattr(pumpfun,'time',SimpleNamespace(time=numeric),raising=False)
    monkeypatch.setattr(pumpfun,'utc_now',observed_utc)
    monkeypatch.setattr(reference,'time',SimpleNamespace(time=lambda:wall[0]))
    monkeypatch.setattr(birth,'time',SimpleNamespace(time=lambda:wall[0]))
    monkeypatch.setattr(native_queue,'time',SimpleNamespace(time=lambda:wall[0]),raising=False)
    return wall,utc,calls


def parse(token):
    return pumpfun._parse_event({'mint':token['address'],'txType':'create',
        'signature':token[reference.FIELD]['tx_hash'],'slot':123})


@pytest.mark.parametrize('offset',[-.001,0.,.000001,.001])
def test_parser_creation_reference_uses_original_numeric_receipt_not_utc_encoding(monkeypatch,offset):
    token,_=candidate()
    wall,utc,calls=clocks(monkeypatch,offset)
    parsed=parse(token)
    assert parsed[reference.FIELD]['received_at']==wall[0]
    assert calls[:2]==['numeric','utc'] and calls.count('numeric')==1
    assert parsed['discovered_at']==utc and parsed['fetched_at']==utc
    assert parsed['created_at'] is None and parsed['age_minutes'] is None
    queued=HotQueue(max_size=2,max_age_min=100,persist_events=False)
    # The queue clock is controlled independently here; its production clock
    # source has a separate strict test below.
    queued._now=lambda:wall[0]
    assert queued.add(parsed)
    delivered=queued.pop_batch(1)[0]
    assert entry_observation.discovery_candidate(delivered)[reference.FIELD]==parsed[reference.FIELD]


@pytest.mark.asyncio
@pytest.mark.parametrize('offset',[0.,.000001,.001])
async def test_native_common_entry_does_not_drop_a_local_reference_when_utc_leads_numeric(monkeypatch,offset):
    token,result=candidate()
    wall,utc,calls=clocks(monkeypatch,offset)
    original_wall=wall[0]
    parsed=parse(token)
    queue=HotQueue(max_size=2,max_age_min=100,persist_events=False)
    queue._now=lambda:wall[0]
    assert queue.add(parsed)
    delivered=queue.pop_batch(1)[0]
    endpoint='https://node.example.test'
    raw=receipt(delivered,result,received=original_wall+.01,
        endpoint_hash=endpoint_fingerprint(endpoint))
    async def lookup(frozen,**kwargs):
        assert frozen==delivered[reference.FIELD]
        wall[0]=original_wall+.01  # An actual later synthetic response, not a receipt rewrite.
        return deepcopy(raw)
    request=AsyncMock(side_effect=lookup)
    owner=RpcTokenBirthResolver(lookup=request,endpoint=lambda:endpoint,degraded=lambda:False,wall=lambda:wall[0])
    monkeypatch.setattr(route,'GLOBAL_RPC_TOKEN_BIRTH_RESOLVER',owner)
    monkeypatch.setenv('BIRTH_ENRICHMENT_ENABLED','true')
    monkeypatch.setenv('BIRTH_RPC_ENRICHMENT_ENABLED','true')
    monkeypatch.setenv('USE_BIRDEYE','false')
    ns,waits,_=preparation_namespace(observed(address=token['address'],price_pct_5m=1000000.))
    ns['_candidate_age_minutes']=compute_age_minutes
    ns['warn_if_nulls']=lambda *args,**kwargs:None
    ns['enrich_token_birth']=route.enrich_token_birth
    prepared=await ns['prepare'](delivered,SimpleNamespace(scalar=AsyncMock(return_value=None)))
    assert prepared is not None and not waits,(waits,owner.snapshot())
    ready,frozen=prepared
    assert ready[birth.FIELD]==raw[birth.FIELD] and ready[reference.FIELD]['received_at']==original_wall
    assert frozen is not None and ready['price_pct_5m']==1000000.
    assert request.await_count==1


@pytest.mark.parametrize('bad',[False,0,float('nan'),float('inf')])
def test_invalid_numeric_receipt_never_falls_back_to_utc_as_a_creation_reference(monkeypatch,bad):
    token,_=candidate()
    monkeypatch.setattr(pumpfun,'time',SimpleNamespace(time=lambda:bad),raising=False)
    parsed=parse(token)
    assert parsed is not None and reference.FIELD not in parsed
    assert parsed['created_at'] is None and parsed['age_minutes'] is None


@pytest.mark.parametrize('offset',[-.000001,0.,.000001])
def test_native_queue_admission_clock_uses_numeric_precision_not_utc_conversion(monkeypatch,offset):
    wall=float(int(time.time())+60)
    calls=[]
    class Datetime(dt.datetime):
        @classmethod
        def now(cls,tz):
            calls.append('utc')
            return dt.datetime.fromtimestamp(wall+offset,tz)
    def numeric():
        calls.append('numeric')
        return wall
    monkeypatch.setattr(native_queue,'dt',SimpleNamespace(datetime=Datetime,timezone=dt.timezone))
    monkeypatch.setattr(native_queue,'time',SimpleNamespace(time=numeric),raising=False)
    assert HotQueue(persist_events=False)._now()==wall
    assert calls==['numeric']
