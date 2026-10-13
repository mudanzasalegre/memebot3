"""Offline native RPC birth route; no real RPC, wallet keys or bot process."""
import asyncio
from copy import deepcopy
import datetime as dt
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from analytics.mint_creation_reference import FIELD as REFERENCE_FIELD, reference_from_event
from analytics.rpc_mint_initialization import VERSION, BASIS, inspect_initialization
from analytics.token_birth import FIELD, checked_token_birth
from test_rpc_mint_initialization import fixture


def candidate():
    result, mint, signature, received = fixture()
    reference = reference_from_event({'txType':'create', 'signature':signature, 'slot':123}, mint, received)
    return {'address':mint, REFERENCE_FIELD:reference}, result


def receipt(token, result, *, received=None, endpoint_hash='a'*64):
    ref = token[REFERENCE_FIELD]
    checked = inspect_initialization(result, token['address'], ref['tx_hash'],
        reference_slot=ref['slot'], reference_received_at=ref['received_at'])
    proof = {'version':VERSION, 'basis':BASIS, 'source':'solana_rpc', 'chain':'solana',
        'address':token['address'], 'created_at':checked['created_at'], 'slot':checked['slot'],
        'tx_hash':checked['tx_hash'], 'received_at':time.time() if received is None else received,
        'rpc_source_sha256':endpoint_hash, 'rpc_evidence_sha256':checked['rpc_evidence_sha256'],
        'evidence':checked['evidence'], 'reference':deepcopy(ref)}
    return {'address':token['address'], 'created_at':dt.datetime.fromisoformat(proof['created_at']), FIELD:proof}


def test_native_checker_accepts_reconstructable_rpc_initialization_receipt():
    token, result = candidate()
    raw = receipt(token, result)
    assert checked_token_birth(raw[FIELD], token['address']) == raw[FIELD]


def test_rpc_receipt_numeric_clock_preserves_equal_and_strictly_future_precision():
    import math
    token, result = candidate()
    received = math.nextafter(time.time(),float('inf'))
    raw = receipt(token,result,received=received)
    assert checked_token_birth(raw[FIELD],token['address'],now=received) is not None
    assert checked_token_birth(raw[FIELD],token['address'],now=math.nextafter(received,-float('inf'))) is None


@pytest.mark.asyncio
async def test_native_router_rpc_is_independent_of_birdeye_enablement(monkeypatch):
    from runtime import token_birth_enrichment as module
    token, result = candidate()
    raw = receipt(token, result)
    rpc = SimpleNamespace(enrich=AsyncMock(return_value={**token, **raw}))
    birdeye = SimpleNamespace(enrich=AsyncMock())
    monkeypatch.setattr(module, 'GLOBAL_RPC_TOKEN_BIRTH_RESOLVER', rpc, raising=False)
    monkeypatch.setattr(module, 'GLOBAL_TOKEN_BIRTH_RESOLVER', birdeye)
    monkeypatch.setenv('BIRTH_ENRICHMENT_ENABLED', 'true')
    monkeypatch.setenv('BIRTH_RPC_ENRICHMENT_ENABLED', 'true')
    monkeypatch.setenv('USE_BIRDEYE', 'false')
    out = await module.enrich_token_birth(token)
    assert out.get(FIELD) == raw[FIELD]
    rpc.enrich.assert_awaited_once()
    birdeye.enrich.assert_not_awaited()


@pytest.mark.parametrize('field,value', [
    ('created_at','2026-01-01T00:00:00+00:00'), ('slot',124), ('tx_hash','1'*64),
    ('source','birdeye'), ('version','wrong'), ('basis','independent_verification'),
    ('rpc_source_sha256','url-with-secret'), ('rpc_evidence_sha256','b'*64),
    ('rpc_source_sha256','A'*64), ('received_at',True), ('received_at',float('inf')),
])
def test_changed_rpc_receipt_is_not_accepted(field, value):
    token, result = candidate()
    proof = receipt(token, result)[FIELD]
    proof[field] = value
    assert checked_token_birth(proof, token['address']) is None


@pytest.mark.parametrize('change', ['reference_mint','reference_signature','reference_slot','reference_future',
                                  'evidence_slot','evidence_time','unsigned','oversized','deep','unexpected_field'])
def test_rpc_receipt_requires_retained_reconstructable_original_evidence(change):
    import base64
    token, result = candidate()
    proof = receipt(token, result)[FIELD]
    if change == 'reference_mint': proof['reference']['address'] = '1'*32
    elif change == 'reference_signature': proof['reference']['tx_hash'] = '1'*64
    elif change == 'reference_slot': proof['reference']['slot'] = 124
    elif change == 'reference_future': proof['reference']['received_at'] = proof['received_at'] + 1
    elif change == 'evidence_slot': proof['evidence']['slot'] += 1
    elif change == 'evidence_time': proof['evidence']['blockTime'] += 1
    elif change == 'unsigned':
        raw = bytearray(base64.b64decode(proof['evidence']['transaction'][0])); raw[1:65] = bytes(64)
        proof['evidence']['transaction'][0] = base64.b64encode(raw).decode()
    elif change == 'oversized': proof['evidence']['meta']['innerInstructions'] = [{'irrelevant':'x'*65536}]
    elif change == 'deep':
        nested = []
        for _ in range(1100): nested = [nested]
        proof['evidence']['meta']['innerInstructions'] = nested
    else: proof['unexpected'] = True
    assert checked_token_birth(proof, token['address']) is None


@pytest.mark.parametrize('lookup,cpi,opcode', [(True,False,0),(True,False,20),(True,True,0),(True,True,20)])
def test_loaded_mint_addresses_and_recorded_cpi_are_supported(lookup, cpi, opcode):
    result, mint, signature, received = fixture(lookup=lookup,cpi=cpi,opcode=opcode)
    checked = inspect_initialization(result, mint, signature, reference_received_at=received)
    assert checked['created_at'] and result['meta']['loadedAddresses']['writable'] == [mint]
    if cpi:
        # Empty data is valid for an unrelated recorded instruction.
        extra = deepcopy(result['meta']['innerInstructions'][0]['instructions'][0])
        extra['data'] = ''
        result['meta']['innerInstructions'][0]['instructions'].append(extra)
        assert inspect_initialization(result,mint,signature,reference_received_at=received)


@pytest.mark.parametrize('fault', [None,'http','redirect','wrong_id','duplicate','error','missing_result',
    'null','malformed','oversized','timeout','connection','wrong_mint','failed','future','missing_time'])
@pytest.mark.asyncio
async def test_native_keyless_creation_rpc_uses_one_strict_original_transaction_query(monkeypatch, fault):
    import aiohttp
    import json
    from fetcher.mint_initialization import get_mint_initialization
    from utils import solana_execution as rpc
    token, result = candidate()
    calls, closed = [], []
    endpoint = 'https://node.example.test/?api-key=synthetic-do-not-leak'
    class Stream:
        def __init__(self, raw): self.raw = raw
        async def read(self, bound):
            part, self.raw = self.raw[:bound], self.raw[bound:]
            return part
    class Reply:
        def __init__(self, request):
            self.status = 401 if fault == 'http' else 302 if fault == 'redirect' else 200
            raw_result = deepcopy(result)
            if fault == 'failed': raw_result['meta']['err'] = {'error':'failed'}
            elif fault == 'future': raw_result['blockTime'] = int(time.time()) + 1
            elif fault == 'missing_time': raw_result['blockTime'] = None
            envelope = {'jsonrpc':'2.0','id':request['id'],'result':None if fault == 'null' else raw_result}
            if fault == 'wrong_id': envelope['id'] = 'different'
            elif fault == 'error': envelope['error'] = {'message':'synthetic-do-not-leak'}
            elif fault == 'missing_result': envelope.pop('result')
            raw = json.dumps(envelope).encode()
            if fault == 'duplicate': raw = raw.replace(b'"jsonrpc": "2.0"',b'"jsonrpc": "2.0", "jsonrpc": "2.0"')
            elif fault == 'malformed': raw = b'{invalid'
            elif fault == 'oversized': raw = b'x'*(2*1024*1024+1)
            self.content = Stream(raw)
        async def __aenter__(self): return self
        async def __aexit__(self,*args): closed.append('reply')
    class Session:
        def __init__(self,**kwargs): assert kwargs['timeout'].total == 3 and 'headers' not in kwargs
        async def __aenter__(self): return self
        async def __aexit__(self,*args): closed.append('session')
        def post(self,url,**kwargs):
            assert url == endpoint and kwargs['allow_redirects'] is False
            calls.append(deepcopy(kwargs['json']))
            if fault == 'timeout': raise asyncio.TimeoutError('synthetic-do-not-leak')
            if fault == 'connection': raise aiohttp.ClientConnectionError('synthetic-do-not-leak')
            return Reply(kwargs['json'])
    if fault == 'wrong_mint': token[REFERENCE_FIELD]['address'] = token['address'] = '1'*32
    monkeypatch.setattr(rpc.aiohttp,'ClientSession',Session)
    if fault in (None,'null'):
        out = await get_mint_initialization(token[REFERENCE_FIELD],endpoint=endpoint)
        if fault == 'null': assert out is None
        else:
            assert out[FIELD]['reference'] == token[REFERENCE_FIELD]
            assert out[FIELD]['rpc_source_sha256'] == rpc.endpoint_fingerprint(endpoint)
            assert checked_token_birth(out[FIELD],token['address']) is not None
    else:
        with pytest.raises((RuntimeError,ValueError)) as error:
            await get_mint_initialization(token[REFERENCE_FIELD],endpoint=endpoint)
        assert 'synthetic-do-not-leak' not in str(error.value)
    assert len(calls) == 1 and 'session' in closed
    assert calls[0]['method'] == 'getTransaction'
    assert calls[0]['params'] == [token[REFERENCE_FIELD]['tx_hash'],
        {'encoding':'base64','commitment':'confirmed','maxSupportedTransactionVersion':0}]


def resolver(lookup, **kwargs):
    from runtime.rpc_token_birth import RpcTokenBirthResolver
    return RpcTokenBirthResolver(lookup=lookup, endpoint=lambda:'https://node.example.test',
        degraded=lambda:False, **kwargs)


@pytest.mark.asyncio
async def test_resolver_retains_detached_positive_cache_after_configuration_changes():
    from utils.solana_execution import endpoint_fingerprint
    token, result = candidate()
    raw = receipt(token,result,endpoint_hash=endpoint_fingerprint('https://node.example.test'))
    lookup = AsyncMock(return_value=raw)
    actual = resolver(lookup)
    first = await actual.enrich(token)
    assert first[FIELD] == raw[FIELD] and first['token_birth_enrichment']['status'] == 'resolved'
    first[FIELD]['slot'] = 999
    actual.endpoint = lambda:'not-an-endpoint'
    second = await actual.enrich(token)
    assert second[FIELD] == raw[FIELD] and second['token_birth_enrichment']['status'] == 'cache_hit'
    assert lookup.await_count == 1
    assert actual.snapshot()['ownership']['owners'] == 0


@pytest.mark.asyncio
async def test_negative_cache_is_bound_to_exact_reference_and_node_without_resetting_budget():
    token, _ = candidate()
    now = [0.]
    lookup = AsyncMock(return_value=None)
    actual = resolver(lookup,rpm=60,clock=lambda:now[0])
    assert (await actual.enrich(token))['token_birth_enrichment']['status'] == 'unavailable'
    now[0] = 1.
    assert (await actual.enrich(token))['token_birth_enrichment']['status'] == 'negative_cache'
    actual.endpoint = lambda:'https://new-node.example.test'
    assert (await actual.enrich(token))['token_birth_enrichment']['status'] == 'unavailable'
    newer = deepcopy(token); newer[REFERENCE_FIELD]['slot'] = None
    assert (await actual.enrich(newer))['token_birth_enrichment']['status'] == 'creation_budget'
    now[0] = 2.
    assert (await actual.enrich(newer))['token_birth_enrichment']['status'] == 'unavailable'
    assert lookup.await_count == 3


@pytest.mark.parametrize('change', ['missing_ref','invalid_ref','known','invalid_clock','future_clock','degraded','bad_endpoint'])
@pytest.mark.asyncio
async def test_resolver_preconditions_do_not_launch_requests(change):
    token, _ = candidate()
    lookup = AsyncMock()
    actual = resolver(lookup)
    if change == 'missing_ref': token.pop(REFERENCE_FIELD)
    elif change == 'invalid_ref': token[REFERENCE_FIELD]['tx_hash'] = 'bad'
    elif change == 'known': token['created_at'] = dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=1)
    elif change == 'invalid_clock': token['created_at'] = 'bad'
    elif change == 'future_clock': token['created_at'] = dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)
    elif change == 'degraded': actual.degraded = lambda:True
    else: actual.endpoint = lambda:'https://user:secret@node.example.test'
    out = await actual.enrich(token)
    lookup.assert_not_awaited()
    assert FIELD not in out and out.get('created_at') == token.get('created_at')


@pytest.mark.parametrize('clock', [True,float('nan'),float('inf'),'0',10**1000,1e308])
@pytest.mark.asyncio
async def test_invalid_admission_clock_never_issues_or_waits_for_rpc(clock):
    token, _ = candidate()
    lookup = AsyncMock()
    out = await resolver(lookup,clock=lambda:clock).enrich(token)
    assert out['token_birth_enrichment']['status'] == 'clock_unavailable'
    lookup.assert_not_awaited()


@pytest.mark.parametrize('clock', [True,float('nan'),float('inf'),'0',0,10**1000])
@pytest.mark.asyncio
async def test_invalid_wall_clock_never_fabricates_birth_or_issues_rpc(clock):
    token, _ = candidate()
    lookup = AsyncMock()
    out = await resolver(lookup,wall=lambda:clock).enrich(token)
    assert out['token_birth_enrichment']['status'] == 'clock_unavailable'
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_loop_joiner_cancellation_does_not_cancel_owner_or_reset_quota():
    from utils.solana_execution import endpoint_fingerprint
    token, result = candidate()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def lookup(reference,**kwargs):
        calls.append(reference); started.set(); await release.wait()
        return receipt(token,result,endpoint_hash=endpoint_fingerprint(kwargs['endpoint']))
    actual = resolver(lookup)
    owner = asyncio.create_task(actual.enrich(token)); await started.wait()
    peer = asyncio.create_task(actual.enrich(token)); await asyncio.sleep(0)
    peer.cancel()
    with pytest.raises(asyncio.CancelledError): await peer
    assert not owner.done() and actual.snapshot()['ownership']['owners'] == 1
    release.set(); out = await owner
    assert FIELD in out and len(calls) == 1
    assert actual.snapshot()['ownership']['coalesced'] == 1
    assert actual.snapshot()['ownership']['loop_buckets'] == 0


@pytest.mark.parametrize('stop', ['cancel','timeout'])
@pytest.mark.asyncio
async def test_repeated_stop_drains_provider_before_ownership_or_cache_release(stop):
    token, _ = candidate()
    started, cleaning, release, ended = [asyncio.Event() for _ in range(4)]
    async def lookup(reference,**kwargs):
        started.set()
        try: await asyncio.Event().wait()
        finally:
            cleaning.set(); await release.wait(); ended.set()
    actual = resolver(lookup,timeout_s=.05 if stop == 'timeout' else 3.)
    owner = asyncio.create_task(actual.enrich(token)); await started.wait()
    if stop == 'cancel': owner.cancel()
    await cleaning.wait(); owner.cancel(); owner.cancel()
    await asyncio.sleep(0); await asyncio.sleep(0)
    assert not owner.done() and not ended.is_set() and actual.snapshot()['ownership']['owners'] == 1
    release.set()
    with pytest.raises(asyncio.CancelledError): await owner
    assert ended.is_set() and actual.snapshot()['ownership']['owners'] == 0
    assert actual.snapshot()['cache_size'] == 0


@pytest.mark.asyncio
async def test_timeout_returns_unknown_only_after_provider_has_cleaned_up():
    token, _ = candidate()
    ended = asyncio.Event()
    async def lookup(reference,**kwargs):
        try: await asyncio.Event().wait()
        finally: ended.set()
    actual = resolver(lookup,timeout_s=.05)
    out = await actual.enrich(token)
    assert ended.is_set() and FIELD not in out and out['token_birth_enrichment']['status'] == 'timeout'
    assert actual.snapshot()['ownership']['loop_buckets'] == 0


@pytest.mark.asyncio
async def test_changed_lookup_context_never_supplies_birth():
    from utils.solana_execution import endpoint_fingerprint
    token, result = candidate()
    raw = receipt(token,result,endpoint_hash=endpoint_fingerprint('https://other-node.example.test'))
    actual = resolver(AsyncMock(return_value=raw))
    out = await actual.enrich(token)
    assert FIELD not in out and out['token_birth_enrichment']['status'] == 'unavailable'


def test_capacity_and_reservations_are_global_across_threads_and_event_loops():
    import concurrent.futures
    import threading
    token, _ = candidate()
    now, calls = [0.], []
    started, release = threading.Event(), threading.Event()
    async def lookup(reference,**kwargs):
        calls.append(reference); started.set()
        while not release.is_set(): await asyncio.sleep(.001)
        return None
    actual = resolver(lookup,clock=lambda:now[0],max_inflight=1,rpm=60)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(lambda:asyncio.run(actual.enrich(token)))
        try:
            assert started.wait(3)
            now[0] = 1.
            out = asyncio.run(actual.enrich(token))
            assert out['token_birth_enrichment']['status'] == 'concurrency_budget'
            assert len(calls) == 1 and actual.snapshot()['ownership']['owners'] == 1
        finally: release.set()
        assert owner.result(timeout=3)['token_birth_enrichment']['status'] == 'unavailable'
    assert actual.snapshot()['ownership']['loop_buckets'] == 0


@pytest.mark.asyncio
async def test_monotonic_pacing_reads_clock_inside_the_reservation_lock():
    token, _ = candidate()
    now = [0.]
    actual = resolver(AsyncMock(return_value=None),rpm=2,negative_ttl_s=1)
    def clock():
        assert actual._lock._is_owned()
        return now[0]
    actual.clock = clock
    assert (await actual.enrich(token))['token_birth_enrichment']['status'] == 'unavailable'
    now[0] = 29.999
    assert (await actual.enrich(token))['token_birth_enrichment']['status'] == 'creation_budget'
    now[0] = 30.
    assert (await actual.enrich(token))['token_birth_enrichment']['status'] == 'unavailable'
    now[0] = 29.
    assert (await actual.enrich(token))['token_birth_enrichment']['status'] == 'clock_unavailable'
    now[0] = 60.
    assert (await actual.enrich(token))['token_birth_enrichment']['status'] == 'unavailable'
    assert actual.lookup.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['master_off','rpc_off','no_reference','rpc_unavailable'])
async def test_native_router_disable_and_birdeye_fallback_preserve_unknown_and_diagnostics(monkeypatch,case):
    from runtime import token_birth_enrichment as module
    token, _ = candidate()
    rpc = SimpleNamespace(enrich=AsyncMock(return_value={**token,'token_birth_enrichment':{
        'source':'solana_rpc','status':'unavailable'}}))
    bird = SimpleNamespace(enrich=AsyncMock(side_effect=lambda value:{**value,
        'token_birth_enrichment':{'source':'birdeye','status':'access_denied'}}))
    monkeypatch.setattr(module,'GLOBAL_RPC_TOKEN_BIRTH_RESOLVER',rpc)
    monkeypatch.setattr(module,'GLOBAL_TOKEN_BIRTH_RESOLVER',bird)
    monkeypatch.setenv('BIRTH_ENRICHMENT_ENABLED','false' if case == 'master_off' else 'true')
    monkeypatch.setenv('BIRTH_RPC_ENRICHMENT_ENABLED','false' if case == 'rpc_off' else 'true')
    monkeypatch.setenv('USE_BIRDEYE','true')
    if case == 'no_reference': token.pop(REFERENCE_FIELD)
    out = await module.enrich_token_birth(token)
    assert FIELD not in out
    if case == 'master_off':
        rpc.enrich.assert_not_awaited(); bird.enrich.assert_not_awaited()
    else:
        bird.enrich.assert_awaited_once()
        if case == 'rpc_unavailable': assert out['token_birth_enrichment']['rpc_initialization']['status'] == 'unavailable'
        else: rpc.enrich.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_discovery_queue_router_fresh_entry_and_T0_keep_checked_rpc_birth(monkeypatch):
    import json
    from fetcher import pumpfun
    from runtime import token_birth_enrichment as module
    from runtime import entry_observation as entry
    from runtime.hot_queue import HotQueue
    from analytics.token_time import compute_age_minutes
    from features.builder import build_feature_vector
    from runtime.trade_learning import freeze_entry_features, validate_entry_features
    from features.auxiliary_semantics import input_frame
    from features.token_clock_semantics import checked_row_clock
    from test_entry_observation import observed, preparation_namespace
    from utils.solana_execution import endpoint_fingerprint
    token, result = candidate()
    ref = token[REFERENCE_FIELD]
    discovered = pumpfun._parse_event({'mint':token['address'],'txType':'create','signature':ref['tx_hash'],'slot':123})
    queue = HotQueue(max_size=2,max_age_min=100,persist_events=False)
    assert queue.add(discovered)
    queued = queue.pop_batch(1)[0]
    raw = receipt(queued,result,endpoint_hash=endpoint_fingerprint('https://node.example.test'))
    rpc = resolver(AsyncMock(return_value=raw))
    monkeypatch.setattr(module,'GLOBAL_RPC_TOKEN_BIRTH_RESOLVER',rpc)
    monkeypatch.setenv('BIRTH_ENRICHMENT_ENABLED','true')
    monkeypatch.setenv('BIRTH_RPC_ENRICHMENT_ENABLED','true')
    monkeypatch.setenv('USE_BIRDEYE','false')
    tick = observed(address=token['address'],price_pct_5m=1000000.)
    ns, waits, _ = preparation_namespace(tick)
    ns['_candidate_age_minutes'] = compute_age_minutes
    ns['warn_if_nulls'] = lambda *a,**k:None
    ns['enrich_token_birth'] = module.enrich_token_birth
    prepared = await ns['prepare'](queued,SimpleNamespace(scalar=AsyncMock(return_value=None)))
    assert prepared is not None and not waits
    ready, observation = prepared
    vector = build_feature_vector(ready)
    assert ready[FIELD] == raw[FIELD] and ready[REFERENCE_FIELD] == queued[REFERENCE_FIELD]
    assert ready['price_pct_5m'] == 1000000. and vector['age_minutes'] > 1
    assert entry.entry_observation_problem(ready,observation,paper=True,vector=vector.to_dict()) is None
    clock = json.loads(vector.attrs['t0_token_clock_proof'])
    assert clock[FIELD] == raw[FIELD]
    assert FIELD not in vector.index and REFERENCE_FIELD not in vector.index
    frozen = freeze_entry_features(vector,address=ready['address'],captured_at=dt.datetime.now(dt.timezone.utc))
    validate_entry_features(frozen,address=ready['address'])
    row = input_frame(vector).iloc[0].to_dict()
    assert checked_row_clock(row)[FIELD] == raw[FIELD]


@pytest.mark.asyncio
async def test_rpc_cache_capacity_and_original_positive_expiry_are_bounded():
    from utils.solana_execution import endpoint_fingerprint
    token, result = candidate()
    origin, now = time.time(), [0.]
    wall = [origin]
    raw = receipt(token,result,received=origin,endpoint_hash=endpoint_fingerprint('https://node.example.test'))
    lookup = AsyncMock(return_value=raw)
    actual = resolver(lookup,clock=lambda:now[0],wall=lambda:wall[0],cache_size=1,rpm=60)
    assert FIELD in await actual.enrich(token)
    expiry = actual._cache[('positive',token['address'])][0]
    now[0], wall[0] = 60., origin + 60
    assert FIELD in await actual.enrich(token)
    assert actual._cache[('positive',token['address'])][0] == expiry
    now[0], wall[0] = 86400., origin + 86400
    assert FIELD in await actual.enrich(token)
    assert lookup.await_count == 2 and actual.snapshot()['cache_size'] == 0


@pytest.mark.asyncio
async def test_rpc_negative_cache_LRU_capacity_never_grows_with_unique_references():
    token, _ = candidate()
    now = [0.]
    actual = resolver(AsyncMock(return_value=None),clock=lambda:now[0],cache_size=2,rpm=60,negative_ttl_s=10)
    for index in range(5):
        now[0] = index
        token[REFERENCE_FIELD]['slot'] = 100 + index
        await actual.enrich(token)
    assert actual.snapshot()['cache_size'] == 2 and actual.lookup.await_count == 5
    now[0] = 14.
    await actual.enrich(token)
    assert actual.snapshot()['cache_size'] == 1 and actual.lookup.await_count == 6


@pytest.mark.asyncio
async def test_global_rolling_minute_budget_and_spacing_are_not_per_mint_or_node():
    token, _ = candidate()
    now = [0.]
    actual = resolver(AsyncMock(return_value=None),clock=lambda:now[0],rpm=60,negative_ttl_s=1)
    for index in range(60):
        now[0] = float(index)
        actual.endpoint = lambda i=index:'https://node-'+str(i)+'.example.test'
        token[REFERENCE_FIELD]['slot'] = 100 + index
        assert (await actual.enrich(token))['token_birth_enrichment']['status'] == 'unavailable'
    now[0] = 59.999
    token[REFERENCE_FIELD]['slot'] = 999
    assert (await actual.enrich(token))['token_birth_enrichment']['status'] == 'creation_budget'
    now[0] = 60.
    assert (await actual.enrich(token))['token_birth_enrichment']['status'] == 'unavailable'
    assert actual.lookup.await_count == 61 and len(actual._reservations) == 60


def test_rpc_invalid_optional_settings_have_safe_importable_bounded_defaults():
    actual = resolver(AsyncMock(),rpm='bad',timeout_s=float('nan'),cache_size=10**1000,
        max_inflight=True,max_joiners='bad',negative_ttl_s=float('inf'))
    snapshot = actual.snapshot()
    assert snapshot['creation_lookup_rpm'] == 12 and snapshot['timeout_s'] == 3
    assert snapshot['cache_size'] == 0 and actual.cache_size == 10000
    assert snapshot['negative_ttl_s'] == 5
    assert snapshot['ownership']['max_owners'] == 2 and snapshot['ownership']['max_joiners'] == 256


@pytest.mark.asyncio
async def test_concurrent_birth_success_cannot_be_rejuvenated_by_a_later_different_initializer():
    from utils.solana_execution import endpoint_fingerprint
    token, result = candidate()
    other = deepcopy(token); other[REFERENCE_FIELD]['slot'] = None
    later = deepcopy(result); later['blockTime'] += 1
    started, release = asyncio.Event(), asyncio.Event()
    now = [0.]
    async def lookup(reference,**kwargs):
        if reference['slot'] is None:
            started.set(); await release.wait()
            return receipt(other,later,endpoint_hash=endpoint_fingerprint(kwargs['endpoint']))
        return receipt(token,result,endpoint_hash=endpoint_fingerprint(kwargs['endpoint']))
    actual = resolver(lookup,clock=lambda:now[0],rpm=60)
    pending = asyncio.create_task(actual.enrich(other)); await started.wait()
    now[0] = 1.
    first = await actual.enrich(token)
    release.set(); second = await pending
    assert first[FIELD] == second[FIELD]
    assert first['created_at'] == second['created_at']
    assert second['token_birth_enrichment']['status'] == 'cache_hit'


def test_runtime_birth_pressure_snapshot_contains_no_mints_endpoints_or_credentials():
    import json
    from runtime.token_birth_enrichment import token_birth_enrichment_snapshot
    state = token_birth_enrichment_snapshot()
    assert 'rpc_initialization' in state and 'resource_access' in state
    encoded = json.dumps(state)
    assert 'https://' not in encoded and 'tx_hash' not in encoded and 'api-key=' not in encoded


def test_missing_optional_rpc_environment_uses_actual_native_global_constructor_defaults(monkeypatch):
    import ast
    from pathlib import Path
    from runtime import token_birth_enrichment as module
    from runtime.rpc_token_birth import RpcTokenBirthResolver
    tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
    assignments = [node for node in tree.body if isinstance(node,ast.Assign)
        and any(isinstance(target,ast.Name) and target.id == 'GLOBAL_RPC_TOKEN_BIRTH_RESOLVER'
            for target in node.targets)]
    assert len(assignments) == 1
    # Execute the real native constructor expression without reloading live
    # globals or editing credentials. This allocates no HTTP/session/owner.
    fake_os = SimpleNamespace(getenv=lambda name,default=None:default)
    namespace = {'RpcTokenBirthResolver':RpcTokenBirthResolver,'os':fake_os}
    exec(compile(ast.Module(body=assignments,type_ignores=[]),module.__file__,'exec'),namespace)
    actual = namespace['GLOBAL_RPC_TOKEN_BIRTH_RESOLVER']
    state = actual.snapshot()
    assert actual.rpm == 12 and actual.timeout_s == 3 and actual.cache_size == 2048
    assert actual.negative_ttl_s == 5 and state['ownership']['max_owners'] == 2
    assert state['ownership']['max_joiners'] == 256 and state['ownership']['owners'] == 0
    monkeypatch.delenv('BIRTH_RPC_ENRICHMENT_ENABLED',raising=False)
    assert module._enabled('BIRTH_RPC_ENRICHMENT_ENABLED') is True
