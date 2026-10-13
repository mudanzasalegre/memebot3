"""Native synthetic HTTP access isolation/recovery; no real key or provider."""
import asyncio
from copy import deepcopy
import json
import time

import pytest

from fetcher import birdeye as be
from runtime.token_birth_enrichment import TokenBirthResolver
from utils import simple_cache as cache

MINT = 'So11111111111111111111111111111111111111112'
OTHER = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
CREATION = '/defi/token_creation_info'
OVERVIEW = '/defi/token_overview'


class Response:
    def __init__(self, status, data=None, gate=None):
        self.status, self.data, self.gate = status, data, gate
    async def __aenter__(self):
        if self.gate is not None:
            await self.gate.wait()
        return self
    async def __aexit__(self, *args):
        pass
    async def json(self):
        return {'success':True, 'data':deepcopy(self.data)}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(be, '_API_KEY', 'synthetic-access-key-not-real')
    if hasattr(be, '_RESOURCE_ACCESS'):
        from utils.provider_access import ResourceAccess
        monkeypatch.setattr(be, '_RESOURCE_ACCESS', ResourceAccess((CREATION, OVERVIEW, be._POOL_EP)))
    cache._CACHE.clear()
    be._fail_count.clear()
    async def throttle(): pass
    monkeypatch.setattr(be, '_throttle', throttle)
    def forbidden(*args, **kwargs):
        raise AssertionError('Real HTTP is forbidden')
    monkeypatch.setattr(be.aiohttp, 'ClientSession', forbidden)
    yield
    assert be._REQUESTS.snapshot()['owners'] == be._REQUESTS.snapshot()['joiners'] == 0
    cache._CACHE.clear()
    be._fail_count.clear()


def install(monkeypatch, responses):
    calls = []
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return responses[min(len(calls)-1, len(responses)-1)]
    monkeypatch.setattr(be.aiohttp, 'ClientSession', lambda *a, **k:Session())
    return calls


@pytest.mark.asyncio
async def test_native_denied_creation_stops_distinct_mint_http_retries(monkeypatch):
    calls = install(monkeypatch, [Response(401)])
    assert await be.get_token_creation_info(MINT, force_refresh=True) is None
    assert await be.get_token_creation_info(OTHER, force_refresh=True) is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_native_birth_enrichment_reports_permission_denial_not_token_absence(monkeypatch):
    calls = install(monkeypatch, [Response(403)])
    resolver = TokenBirthResolver(degraded=lambda:False)
    result = await resolver.enrich({'address':MINT})
    assert result['token_birth_enrichment']['status'] == 'access_denied'
    assert 'created_at' not in result and 'token_birth_observation' not in result
    assert len(calls) == 1 and resolver.snapshot()['cache_size'] == 0


def use_clock(monkeypatch, now, cooldown=5):
    from utils.provider_access import ResourceAccess
    guard = ResourceAccess((CREATION, OVERVIEW, be._POOL_EP), cooldown, clock=lambda:now[0])
    monkeypatch.setattr(be, '_RESOURCE_ACCESS', guard)
    return guard


def creation(address=MINT):
    return {'tokenAddress':address, 'slot':42, 'txHash':'1'*64, 'blockUnixTime':int(time.time())-180}


def overview(address=MINT):
    return {'address':address, 'price':2, 'liquidity':10000, 'priceChange5mPercent':25000}


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403])
async def test_denial_is_fixed_resource_scoped_and_never_logs_response_body(monkeypatch, caplog, status):
    calls = install(monkeypatch, [Response(status), Response(200, overview())])
    with caplog.at_level('WARNING', logger='birdeye'):
        assert await be.get_token_creation_info(MINT) is None
        for _ in range(20):
            assert await be.get_token_creation_info(OTHER, force_refresh=True) is None
        assert (await be.get_token_info(MINT))['price_pct_5m'] == 25000
    assert len(calls) == 2 and len(caplog.records) == 1
    assert be.creation_resource_status()['status'] == 'access_denied'
    raw = json.dumps(be.request_runtime_snapshot()) + caplog.text
    assert be._API_KEY not in raw and MINT not in raw and OTHER not in raw
    assert set(be.request_runtime_snapshot()['resource_access']) == {CREATION, OVERVIEW, be._POOL_EP}
    assert not be._fail_count


@pytest.mark.asyncio
async def test_recovery_deadline_is_exact_and_force_does_not_bypass(monkeypatch):
    now = [0.]
    use_clock(monkeypatch, now)
    calls = install(monkeypatch, [Response(401), Response(200, creation(OTHER))])
    assert await be.get_token_creation_info(MINT) is None
    now[0] = 4.999
    assert await be.get_token_creation_info(OTHER, force_refresh=True) is None
    assert len(calls) == 1
    now[0] = 5.
    assert be.creation_resource_status()['status'] == 'retry_due'
    result = await be.get_token_creation_info(OTHER)
    assert result['token_birth_observation']['address'] == OTHER
    assert be.creation_resource_status()['status'] == 'available' and len(calls) == 2


@pytest.mark.asyncio
async def test_recovery_allows_only_one_http_probe_and_other_resources_stay_available(monkeypatch):
    now, gate = [0.], asyncio.Event()
    use_clock(monkeypatch, now)
    calls = install(monkeypatch, [Response(403), Response(200, creation(), gate), Response(200, overview(OTHER))])
    assert await be.get_token_creation_info(MINT) is None
    now[0] = 5.
    probe = asyncio.create_task(be.get_token_creation_info(MINT, force_refresh=True))
    for _ in range(20): await asyncio.sleep(0)
    assert be.creation_resource_status()['status'] == 'access_probe_inflight'
    assert await be.get_token_creation_info(OTHER, force_refresh=True) is None
    assert (await be.get_token_info(OTHER))['price_usd'] == 2
    assert len(calls) == 3
    gate.set()
    assert (await probe)['address'] == MINT
    assert be.creation_resource_status()['status'] == 'available'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,data', [(500,None),(429,None),(200,{'tokenAddress':OTHER}),(404,None)])
async def test_bad_recovery_response_does_not_certify_access_or_poison_token_cache(monkeypatch, status, data):
    now = [0.]
    use_clock(monkeypatch, now)
    calls = install(monkeypatch, [Response(401),Response(status,data)])
    await be.get_token_creation_info(MINT)
    now[0] = 5.
    assert await be.get_token_creation_info(OTHER,force_refresh=True) is None
    health = be.creation_resource_status()
    assert health['status'] == 'access_recovery_wait' and health['last_denied_http_status'] == 401
    assert health['last_http_status'] == status and health['retry_after_s'] == 5.
    assert not be._fail_count and cache.cache_get(f'be:v2:creation:{OTHER}') is None
    assert await be.get_token_creation_info(MINT, force_refresh=True) is None and len(calls) == 2


@pytest.mark.asyncio
async def test_cancelled_recovery_probe_releases_all_owned_state(monkeypatch):
    now, gate = [0.], asyncio.Event()
    use_clock(monkeypatch, now)
    calls = install(monkeypatch,[Response(401),Response(200,creation(),gate),Response(200,creation(OTHER))])
    await be.get_token_creation_info(MINT)
    now[0] = 5.
    task = asyncio.create_task(be.get_token_creation_info(MINT,force_refresh=True))
    for _ in range(20): await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert be.creation_resource_status()['status'] == 'access_recovery_wait'
    assert be.request_runtime_snapshot()['latest_generations'] == 0
    assert be._REQUESTS.snapshot()['owners'] == 0 and not be._fail_count
    now[0] = 10.
    assert (await be.get_token_creation_info(OTHER))['address'] == OTHER and len(calls) == 3


@pytest.mark.asyncio
async def test_key_rotation_observed_before_throttle_completion_sends_no_obsolete_credential(monkeypatch):
    calls = install(monkeypatch,[Response(200,creation())])
    async def rotate(): be._API_KEY = 'new-synthetic-access-key'
    monkeypatch.setattr(be,'_throttle',rotate)
    assert await be.get_token_creation_info(MINT) is None
    assert not calls and not be._fail_count
    assert be.creation_resource_status()['status'] == 'unknown'


@pytest.mark.asyncio
async def test_new_key_does_not_join_obsolete_request_or_inherit_its_late_denial(monkeypatch):
    gate = asyncio.Event()
    calls = install(monkeypatch,[Response(401,gate=gate),Response(200,creation())])
    old = asyncio.create_task(be.get_token_creation_info(MINT))
    for _ in range(20): await asyncio.sleep(0)
    be._API_KEY = 'new-synthetic-access-key'
    current = await be.get_token_creation_info(MINT)
    gate.set()
    assert await old is None and current['address'] == MINT
    assert len(calls) == 2 and calls[0][1]['headers']['X-API-KEY'] != calls[1][1]['headers']['X-API-KEY']
    assert be.creation_resource_status()['status'] == 'available' and not be._fail_count
    assert (await be.get_token_creation_info(MINT))['token_birth_observation'] == current['token_birth_observation']


@pytest.mark.asyncio
async def test_denied_state_recovers_with_new_in_memory_key_without_old_negative_cache(monkeypatch):
    calls = install(monkeypatch,[Response(403),Response(200,creation())])
    resolver = TokenBirthResolver(degraded=lambda:False)
    first = await resolver.enrich({'address':MINT,'first_seen':123})
    assert first['token_birth_enrichment']['status'] == 'access_denied'
    assert resolver.snapshot()['cache_size'] == 0
    be._API_KEY = 'new-synthetic-access-key'
    # Keep the existing creation-only spacing. Credential rotation is not a
    # license to bypass that independent budget.
    resolver._next_request_at = None
    result = await resolver.enrich({'address':MINT,'first_seen':123})
    assert result['token_birth_enrichment']['status'] == 'resolved'
    assert result['first_seen'] == 123 and len(calls) == 2


@pytest.mark.asyncio
async def test_birth_negative_cache_changes_generation_but_original_positive_context_remains(monkeypatch):
    now = [0.]
    use_clock(monkeypatch, now)
    calls = install(monkeypatch,[Response(500),Response(200,creation()),Response(401)])
    resolver = TokenBirthResolver(degraded=lambda:False,rpm=60,clock=lambda:now[0])
    first = await resolver.enrich({'address':MINT})
    assert first['token_birth_enrichment']['status'] == 'unavailable'
    be._API_KEY = 'new-synthetic-access-key'
    now[0] = 1.
    born = await resolver.enrich({'address':MINT})
    assert born['token_birth_enrichment']['status'] == 'resolved'
    original = deepcopy(born['token_birth_observation'])
    now[0] = 2.
    denied = await resolver.enrich({'address':OTHER})
    assert denied['token_birth_enrichment']['status'] == 'access_denied'
    cached = await resolver.enrich({'address':MINT})
    assert cached['token_birth_enrichment']['status'] == 'cache_hit'
    assert cached['token_birth_observation'] == original and len(calls) == 3


def test_pure_health_latest_admission_and_configuration_aba_cannot_be_overwritten():
    from utils.provider_access import ResourceAccess
    access = ResourceAccess((CREATION,OVERVIEW))
    old = access.admit(CREATION,'A')
    latest = access.admit(CREATION,'A')
    assert access.finish(latest,200,valid_response=True)
    assert not access.finish(old,401)
    assert access.status(CREATION,'A')['status'] == 'available'
    access.status(CREATION,'B')
    again = access.admit(CREATION,'A')
    assert not access.finish(old,403)
    assert access.finish(again,200,valid_response=True)
    assert access.status(CREATION,'A')['status'] == 'available'


def test_pure_recovery_probe_admission_is_thread_atomic():
    from concurrent.futures import ThreadPoolExecutor
    from utils.provider_access import ResourceAccess
    now = [0.]
    access = ResourceAccess((CREATION,),5,clock=lambda:now[0])
    assert access.finish(access.admit(CREATION,'A'),401)
    now[0] = 5.
    with ThreadPoolExecutor(max_workers=8) as workers:
        leases = list(workers.map(lambda _:access.admit(CREATION,'A'),range(64)))
    leases = [lease for lease in leases if lease is not None]
    assert len(leases) == 1
    access.release(leases[0])
    assert access.status(CREATION,'A')['status'] == 'access_recovery_wait'


@pytest.mark.parametrize('bad', [True,float('nan'),float('inf'),'bad'])
def test_invalid_local_clock_fails_closed_and_recovers_without_infinite_lock(bad):
    from utils.provider_access import ResourceAccess
    now = [0.]
    access = ResourceAccess((CREATION,),5,clock=lambda:now[0])
    lease = access.admit(CREATION,'A')
    now[0] = bad
    assert access.finish(lease,401)
    assert access.admit(CREATION,'A') is None
    assert access.status(CREATION,'A')['status'] == 'clock_unavailable'
    now[0] = 1.
    assert access.status(CREATION,'A')['retry_after_s'] == 5.
    now[0] = 6.
    assert access.admit(CREATION,'A') is not None


def test_clock_reversal_keeps_denial_bounded_not_rejuvenated_access():
    from utils.provider_access import ResourceAccess
    now = [100.]
    access = ResourceAccess((CREATION,),5,clock=lambda:now[0])
    access.finish(access.admit(CREATION,'A'),403)
    now[0] = 99.
    assert access.status(CREATION,'A')['status'] == 'access_denied'
    assert access.status(CREATION,'A')['retry_after_s'] == 5.
    now[0] = 104.
    assert access.admit(CREATION,'A') is not None


@pytest.mark.parametrize('value,expected',[('bad',300),(True,300),(0,1),(-1,1),(99999,3600),(float('nan'),300)])
def test_retry_budget_is_bounded_and_bad_configuration_cannot_crash(value,expected):
    from utils.provider_access import ResourceAccess
    assert ResourceAccess((CREATION,),value).cooldown_s == expected


def test_diagnostics_are_detached_fixed_capacity_and_do_not_retain_unknown_resources():
    from utils.provider_access import ResourceAccess
    access = ResourceAccess((CREATION,OVERVIEW))
    key = 'private-synthetic-do-not-output'
    for i in range(1000):
        assert access.admit('unsupported-'+str(i),key) is None
    lease = access.admit(CREATION,key)
    access.finish(lease,401)
    snapshot = access.snapshot(key)
    assert len(access._states) == 1 and set(snapshot) == {CREATION,OVERVIEW}
    assert key not in json.dumps(snapshot) and key not in repr(lease)
    snapshot[CREATION]['status'] = 'available'
    assert access.status(CREATION,key)['status'] == 'access_denied'


@pytest.mark.asyncio
async def test_throttled_older_request_does_not_send_after_newer_resource_denial(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    count = [0]
    async def throttle():
        count[0] += 1
        if count[0] == 1:
            started.set()
            await release.wait()
    monkeypatch.setattr(be,'_throttle',throttle)
    calls = install(monkeypatch,[Response(401)])
    waiting = asyncio.create_task(be.get_token_creation_info(MINT))
    await started.wait()
    assert await be.get_token_creation_info(OTHER) is None
    release.set()
    assert await waiting is None and len(calls) == 1
    assert not be._fail_count


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[429,500])
async def test_transient_native_negative_cache_is_not_inherited_by_new_key(monkeypatch,status):
    calls = install(monkeypatch,[Response(status),Response(200,creation())])
    assert await be.get_token_creation_info(MINT) is None
    assert await be.get_token_creation_info(MINT) is None and len(calls) == 1
    be._API_KEY = 'new-synthetic-access-key'
    assert (await be.get_token_creation_info(MINT))['address'] == MINT
    assert len(calls) == 2 and not be._fail_count


@pytest.mark.asyncio
async def test_native_main_runtime_pressure_contains_resource_health_without_new_requests(monkeypatch):
    calls = install(monkeypatch,[Response(403)])
    await be.get_token_creation_info(MINT)
    health = be.request_runtime_snapshot()
    assert health['resource_access'][CREATION]['status'] == 'access_denied'
    resolver = TokenBirthResolver(degraded=lambda:False)
    for address in (MINT,OTHER):
        enriched = await resolver.enrich({'address':address})
        assert enriched['token_birth_enrichment']['status'] == 'access_denied'
    assert resolver.snapshot()['resource_access']['last_denied_http_status'] == 403
    assert not resolver._reservations and len(calls) == 1


@pytest.mark.parametrize('http_status,valid',[(200,False),(500,True),(200,1),(401.0,True),(True,True)])
def test_health_does_not_accept_unvalidated_or_malformed_success(http_status,valid):
    from utils.provider_access import ResourceAccess
    access = ResourceAccess((CREATION,))
    assert access.finish(access.admit(CREATION,'A'),http_status,valid_response=valid)
    assert access.status(CREATION,'A')['status'] == 'unknown'
