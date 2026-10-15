"""Native merge causality keeps the precision of the actual clock source."""
from copy import deepcopy
import datetime as dt
import math
from types import SimpleNamespace
import time

import pytest

from analytics import token_birth as birth

MINT='So11111111111111111111111111111111111111112'


def original_receipt(source,received):
    if source=='rpc':
        from test_rpc_birth_enrichment import candidate,receipt
        token,result=candidate()
        raw=receipt(token,result,received=received)
        return token['address'],raw
    born=dt.datetime.fromtimestamp(int(received)-180,dt.timezone.utc)
    proof={'version':birth.VERSION,'source':'birdeye','chain':'solana',
        'address':MINT,'created_at':born.isoformat(),'slot':123,'tx_hash':'1'*64,
        'received_at':received,'basis':birth.BASIS}
    return MINT,{'address':MINT,'created_at':born,birth.FIELD:proof}


def clock_pair(direction):
    # Whole epoch seconds are exactly represented. One adjacent float lies
    # inside datetime's microsecond cell, with strictly different causality.
    base=float(int(time.time())+60)
    wall=math.nextafter(base,math.inf if direction=='down' else -math.inf)
    utc=dt.datetime.fromtimestamp(wall,dt.timezone.utc)
    assert utc.timestamp()==base
    assert (utc.timestamp()<wall) if direction=='down' else (utc.timestamp()>wall)
    return wall,utc


def install_clocks(monkeypatch,wall,utc):
    calls=[]
    class Datetime:
        @staticmethod
        def now(tz):
            assert tz==dt.timezone.utc
            calls.append('datetime')
            return utc
    def numeric():
        calls.append('numeric')
        return wall
    monkeypatch.setattr(birth,'dt',SimpleNamespace(datetime=Datetime,timezone=dt.timezone))
    monkeypatch.setattr(birth,'time',SimpleNamespace(time=numeric))
    return calls


@pytest.mark.parametrize('source',['birdeye','rpc'])
@pytest.mark.parametrize('direction',['down','up'])
def test_default_merge_uses_one_actual_numeric_clock_without_rounding_receipts(monkeypatch,source,direction):
    wall,utc=clock_pair(direction)
    received=wall if direction=='down' else utc.timestamp()
    mint,raw=original_receipt(source,received)
    original=deepcopy(raw)
    assert birth.checked_token_birth(raw[birth.FIELD],mint,now=received) is not None
    assert (birth.checked_token_birth(raw[birth.FIELD],mint,now=wall) is not None)==(direction=='down')
    calls=install_clocks(monkeypatch,wall,utc)
    primary={'address':mint,'discovered_via':'dex'}
    out=birth.merge_birth_context(primary,raw)
    if direction=='down':
        assert out.get(birth.FIELD)==raw[birth.FIELD]
        assert out['created_at']==raw['created_at']
        out[birth.FIELD]['slot']=999
        assert raw==original
    else:
        assert out==primary
    assert calls==['numeric']
    assert primary=={'address':mint,'discovered_via':'dex'} and raw==original


@pytest.mark.parametrize('source',['birdeye','rpc'])
@pytest.mark.parametrize('direction',['down','up'])
def test_explicit_numeric_merge_keeps_equal_and_strictly_future_receipts_distinct(source,direction):
    wall,utc=clock_pair(direction)
    received=wall if direction=='down' else utc.timestamp()
    mint,raw=original_receipt(source,received)
    primary={'address':mint}
    out=birth.merge_birth_context(primary,raw,now=wall)
    assert (birth.FIELD in out)==(direction=='down')
    if direction=='down': assert out[birth.FIELD]==raw[birth.FIELD]
    else: assert out==primary


@pytest.mark.parametrize('explicit',[False,True])
def test_untyped_original_birth_cannot_be_a_future_microsecond_rounded_into_now(monkeypatch,explicit):
    wall,utc=clock_pair('up')
    incoming={'address':MINT,'created_at':utc}
    primary={'address':MINT}
    calls=install_clocks(monkeypatch,wall,utc)
    out=birth.merge_birth_context(primary,incoming,**({'now':wall} if explicit else {}))
    assert out==primary and incoming['created_at'].timestamp()>wall
    assert calls==([] if explicit else ['numeric'])


@pytest.mark.parametrize('alias',birth.BIRTH_CLOCK_FIELDS)
def test_known_original_clock_is_unchanged_and_needs_no_new_clock_read(monkeypatch,alias):
    mint,raw=original_receipt('birdeye',time.time())
    original={'address':mint,alias:raw['created_at']-dt.timedelta(days=1)}
    def forbidden(*args,**kwargs): raise AssertionError('Known original clocks do not need another now')
    monkeypatch.setattr(birth,'time',SimpleNamespace(time=forbidden))
    monkeypatch.setattr(birth,'dt',SimpleNamespace(datetime=SimpleNamespace(now=forbidden),timezone=dt.timezone))
    assert birth.merge_birth_context(original,raw)==original


@pytest.mark.parametrize('now',[False,0,float('nan'),float('inf'),'not-a-clock'])
def test_invalid_explicit_merge_clock_stays_unknown(now):
    mint,raw=original_receipt('birdeye',time.time())
    assert birth.merge_birth_context({'address':mint},raw,now=now)=={'address':mint}


@pytest.mark.parametrize('kind',['seconds','milliseconds','microseconds','nanoseconds','datetime','iso'])
def test_explicit_supported_clock_encodings_preserve_original_receipt(kind):
    base=float(int(time.time())+60)
    mint,raw=original_receipt('birdeye',base)
    now={'seconds':base,'milliseconds':int(base*1000),'microseconds':int(base*1e6),
         'nanoseconds':int(base*1e9),'datetime':dt.datetime.fromtimestamp(base,dt.timezone.utc),
         'iso':dt.datetime.fromtimestamp(base,dt.timezone.utc).isoformat()}[kind]
    assert birth.merge_birth_context({'address':mint},raw,now=now)[birth.FIELD]==raw[birth.FIELD]
