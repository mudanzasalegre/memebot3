"""Original checked birth reuse across discovery sources; no provider calls."""
from copy import deepcopy
import datetime as dt
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from analytics.token_birth import FIELD,checked_token_birth
from runtime import token_birth_enrichment as route
from test_rpc_birth_enrichment import candidate,receipt,resolver
from utils.solana_execution import endpoint_fingerprint

ENDPOINT='https://node.example.test'


async def seeded(**kwargs):
    token,result=candidate()
    raw=receipt(token,result,endpoint_hash=endpoint_fingerprint(ENDPOINT))
    lookup=AsyncMock(return_value=raw)
    owner=resolver(lookup,**kwargs)
    first=await owner.enrich(token)
    assert first.get(FIELD)==raw[FIELD]
    return token,raw,owner,lookup


def enable(monkeypatch,owner):
    bird=SimpleNamespace(enrich=AsyncMock(side_effect=lambda value:{**deepcopy(value),
        'token_birth_enrichment':{'source':'birdeye','status':'access_denied'}}))
    monkeypatch.setattr(route,'GLOBAL_RPC_TOKEN_BIRTH_RESOLVER',owner)
    monkeypatch.setattr(route,'GLOBAL_TOKEN_BIRTH_RESOLVER',bird)
    monkeypatch.setenv('BIRTH_ENRICHMENT_ENABLED','true')
    monkeypatch.setenv('BIRTH_RPC_ENRICHMENT_ENABLED','true')
    monkeypatch.setenv('USE_BIRDEYE','true')
    return bird


@pytest.mark.asyncio
async def test_original_positive_cache_does_not_need_a_new_discovery_reference():
    token,raw,owner,lookup=await seeded()
    incoming={'address':token['address'],'discovered_via':'dex'}
    original=deepcopy(incoming)
    expiry=owner._cache[('positive',token['address'])][0]
    out=await owner.enrich(incoming)
    assert out.get(FIELD)==raw[FIELD]
    assert out['token_birth_enrichment']['status']=='cache_hit'
    assert incoming==original and lookup.await_count==1
    assert owner._cache[('positive',token['address'])][0]==expiry
    out[FIELD]['slot']=999
    assert owner._cache[('positive',token['address'])][1][FIELD]==raw[FIELD]


@pytest.mark.asyncio
@pytest.mark.parametrize('source',['dex','revival','pumpfun'])
async def test_real_router_reuses_original_rpc_receipt_without_birdeye_or_new_rpc(monkeypatch,source):
    token,raw,owner,lookup=await seeded()
    bird=enable(monkeypatch,owner)
    def forbidden(): raise AssertionError('Cached birth needs no endpoint or provider access')
    owner.endpoint=owner.degraded=forbidden
    incoming={'address':token['address'],'discovered_via':source}
    out=await route.enrich_token_birth(incoming)
    assert out.get(FIELD)==raw[FIELD] and checked_token_birth(out[FIELD],token['address'])==raw[FIELD]
    assert out['token_birth_enrichment']['status']=='cache_hit'
    assert lookup.await_count==1
    bird.enrich.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_miss_without_reference_cannot_create_a_lookup_or_birth(monkeypatch):
    token,_=candidate()
    lookup=AsyncMock()
    owner=resolver(lookup)
    bird=enable(monkeypatch,owner)
    out=await route.enrich_token_birth({'address':token['address']})
    assert FIELD not in out and 'created_at' not in out
    lookup.assert_not_awaited()
    bird.enrich.assert_awaited_once()
    assert owner.snapshot()['cache_size']==0 and not owner._reservations


@pytest.mark.asyncio
@pytest.mark.parametrize('flag',['BIRTH_ENRICHMENT_ENABLED','BIRTH_RPC_ENRICHMENT_ENABLED'])
async def test_disable_flags_cannot_be_bypassed_by_a_positive_cache(monkeypatch,flag):
    token,raw,owner,lookup=await seeded()
    bird=enable(monkeypatch,owner)
    monkeypatch.setenv(flag,'false')
    out=await route.enrich_token_birth({'address':token['address']})
    assert FIELD not in out and lookup.await_count==1
    assert bird.enrich.await_count==(0 if flag=='BIRTH_ENRICHMENT_ENABLED' else 1)


@pytest.mark.asyncio
async def test_native_common_entry_and_frozen_T0_accept_only_the_original_cached_proof(monkeypatch):
    from analytics.token_time import compute_age_minutes
    from features.builder import build_feature_vector
    from runtime import entry_observation as entry
    from runtime.trade_learning import freeze_entry_features,validate_entry_features
    from test_entry_observation import observed,preparation_namespace
    token,raw,owner,lookup=await seeded()
    bird=enable(monkeypatch,owner)
    snapshot=observed(address=token['address'],price_pct_5m=1000000.)
    namespace,waits,_=preparation_namespace(snapshot)
    namespace['_candidate_age_minutes']=compute_age_minutes
    namespace['warn_if_nulls']=lambda *args,**kwargs:None
    namespace['enrich_token_birth']=route.enrich_token_birth
    incoming={'address':token['address'],'discovered_via':'dex','discovered_at':time.time()-20}
    prepared=await namespace['prepare'](incoming,SimpleNamespace(scalar=AsyncMock(return_value=None)))
    assert prepared is not None,waits
    ready,observation=prepared
    vector=build_feature_vector(ready)
    assert ready[FIELD]==raw[FIELD] and ready['price_pct_5m']==1000000.
    assert entry.entry_observation_problem(ready,observation,paper=True,vector=vector.to_dict()) is None
    frozen=freeze_entry_features(vector,address=ready['address'],captured_at=dt.datetime.now(dt.timezone.utc))
    validate_entry_features(frozen,address=ready['address'])
    assert FIELD not in vector.index and lookup.await_count==1
    bird.enrich.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['invalid_clock','future_clock','known_clock','partial_receipt','wrong_mint','bad_identity','tampered_cache','expired_cache'])
async def test_cache_only_reuse_cannot_repair_or_fabricate_original_evidence(fault):
    mono=[0.]
    token,raw,owner,lookup=await seeded(clock=lambda:mono[0])
    incoming={'address':token['address']}
    if fault=='invalid_clock': incoming['created_at']='invalid'
    elif fault=='future_clock': incoming['created_at']=dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)
    elif fault=='known_clock': incoming['created_at']=dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=1)
    elif fault=='partial_receipt': incoming[FIELD]={'source':'unverified'}
    elif fault=='wrong_mint': incoming['address']='1'*32
    elif fault=='bad_identity': incoming['address']={'not':'a mint'}
    elif fault=='tampered_cache': owner._cache[('positive',token['address'])][1][FIELD]['slot']+=1
    elif fault=='expired_cache': mono[0]=owner._cache[('positive',token['address'])][0]+1
    original=deepcopy(incoming)
    assert owner.cached_birth(incoming) is None
    assert incoming==original and lookup.await_count==1
    assert len(owner._reservations)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('wall',[True,0,float('nan'),float('inf')])
async def test_cached_birth_unknown_clock_cannot_authorize_reuse_or_new_lookup(wall):
    token,raw,owner,lookup=await seeded()
    owner.wall=lambda:wall
    assert owner.cached_birth({'address':token['address']}) is None
    assert lookup.await_count==1


@pytest.mark.asyncio
async def test_negative_cache_without_reference_cannot_be_promoted_to_a_birth():
    token,_=candidate()
    lookup=AsyncMock(return_value=None)
    owner=resolver(lookup)
    assert FIELD not in await owner.enrich(token)
    before=list(owner._reservations)
    assert owner.cached_birth({'address':token['address']}) is None
    out=await owner.enrich({'address':token['address']})
    assert FIELD not in out and out['token_birth_enrichment']['status']=='missing_creation_reference'
    assert lookup.await_count==1 and list(owner._reservations)==before


@pytest.mark.asyncio
@pytest.mark.parametrize('address',[None,False,123,[],{},'invalid'])
async def test_direct_unreferenced_invalid_identity_stays_unknown_without_lookup(address):
    lookup=AsyncMock()
    owner=resolver(lookup)
    out=await owner.enrich({'address':address})
    assert FIELD not in out and out['token_birth_enrichment']['status']=='missing_creation_reference'
    lookup.assert_not_awaited()
    assert not owner._cache and not owner._reservations
