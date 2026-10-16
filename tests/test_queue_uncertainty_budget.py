"""Native retry consumers after synthetic uncertainty; no provider or orders."""
import ast
from copy import deepcopy
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock

import pytest

from analytics import requeue_policy
from analytics.token_time import BIRTH_CLOCK_FIELDS,compute_age_minutes
from analytics.token_birth import FIELD as BIRTH_FIELD,merge_birth_context
from utils import lista_pares as queue
from test_entry_observation import observed,preparation_namespace,run_function,MINT

WAITS=('entry_observation:missing_age','entry_observation:snapshot_unavailable',
    'strategy:confirm_snapshots','live_profit_gate:temporarily_unavailable')


@pytest.fixture
def environment(monkeypatch):
    now=[dt.datetime.now(dt.timezone.utc).timestamp()]
    monkeypatch.setattr(queue,'_pair_watch',{});monkeypatch.setattr(queue,'_processed',set())
    monkeypatch.setattr(queue,'time',SimpleNamespace(time=lambda:now[0]))
    for name in ('log_queue_add','log_queue_requeue','log_queue_drop','_persist'):
        monkeypatch.setattr(queue,name,Mock())
    monkeypatch.setattr(queue,'MAX_QUEUE_SIZE',3);monkeypatch.setattr(queue,'MAX_INCOMPLETE_SEC',600)
    monkeypatch.setattr(queue,'MAX_RETRIES',5)
    monkeypatch.setattr(queue,'NON_DECREMENT_REASON_PREFIXES',('strategy:confirm_snapshots','live_profit_gate:'))
    monkeypatch.setenv('TRADING_HOURS','');monkeypatch.setenv('TRADING_HOURS_EXTRA','')
    monkeypatch.setattr(requeue_policy,'utc_now',lambda:dt.datetime.fromtimestamp(now[0],dt.timezone.utc))
    for name,value in [('MIN_AGE_MIN',1),('MAX_AGE_DAYS',7),('MIN_MARKET_CAP_USD',1000),
        ('MAX_MARKET_CAP_USD',1000000),('MIN_VOL_USD_24H',1000),('MIN_LIQUIDITY_USD',1000)]:
        monkeypatch.setattr(requeue_policy,name,value)
    return now


def waited(environment,reason,count=12,retries=5):
    assert queue.agregar_si_nuevo(MINT,retries=retries)
    for _ in range(count):
        assert queue.requeue(MINT,reason=reason,backoff=1)
        environment[0]+=2
    assert queue.retries_left(MINT)==retries
    assert queue.meta(MINT)['attempts']==count


def basic_native_branch():
    node=run_function('_evaluate_and_buy')
    branch=next(n for n in node.body if isinstance(n,ast.If)
        and any(isinstance(c,ast.Call) and ast.unparse(c.func)=='requeue_policy.decide' for c in ast.walk(n)))
    wrapper=ast.parse('def check(token,addr):\n    pass').body[0]
    wrapper.body=[deepcopy(branch)]
    ns={'lista_pares':queue,'requeue_policy':requeue_policy,'time':queue.time,
        'green_fast_path':False,'moonshot_fast_path':False,'shadow_followup_fast_path':False,
        'sniper_micro_fallback_probe':False,'paper_bootstrap_fast_path':False,'basic_filter_result':False,
        '_stats':{'filtered_out':0},'_research_decision':Mock(),'_store_policy_reject':Mock(),
        '_remember_stream_candidate_cooldown':Mock(),'_stream_candidate_cooldown_s':lambda *a:30,
        '_remove_from_queue_if_present':queue.eliminar_par,
        '_requeue_with_stats':lambda addr,token=None,**kw:queue.requeue(addr,**kw)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),'run_bot.py','exec'),ns)
    return ns


@pytest.mark.parametrize('wait',WAITS)
@pytest.mark.parametrize('metric',['too_young','mcap_low','vol_low','liq_low'])
def test_native_basic_filter_keeps_its_original_reconsideration_budget_after_uncertainty(environment,wait,metric):
    waited(environment,wait)
    token={'address':MINT,'age_minutes':3,'market_cap_usd':50000,'volume_24h_usd':10000,'liquidity_usd':10000}
    token[{'too_young':'age_minutes','mcap_low':'market_cap_usd','vol_low':'volume_24h_usd','liq_low':'liquidity_usd'}[metric]]=0.1
    ns=basic_native_branch();ns['check'](token,MINT)
    assert queue.meta(MINT) is not None, 'Temporary observation waits exhausted a different policy budget'
    assert queue.retries_left(MINT)==4 and MINT not in queue._processed
    assert ns['_stats']['filtered_out']==0
    ns['_store_policy_reject'].assert_not_called()
    assert ns['_research_decision'].call_args.kwargs['action']=='wait'


@pytest.mark.asyncio
@pytest.mark.parametrize('wait',WAITS)
async def test_native_zero_liquidity_retries_not_exhausted_by_missing_data_waits(environment,wait):
    waited(environment,wait)
    ns,waits,market=zero_liquidity_native(environment)
    token={'address':MINT,'discovered_via':'dex','created_at':dt.datetime.fromtimestamp(environment[0]-180,dt.timezone.utc)}
    assert await ns['prepare'](token,SimpleNamespace(scalar=AsyncMock(return_value=None))) is None
    assert queue.meta(MINT) is not None, 'Observation waits spent native no-liquidity retries'
    assert queue.retries_left(MINT)==4 and MINT not in queue._processed
    assert ns['_stats']['incomplete']==1 and not waits
    # Measured zero liquidity remains a negative observation, not a buy bypass.
    assert ns['_store_policy_reject'].call_args.kwargs['reason']=='no_liq'


def zero_liquidity_native(environment):
    ns,waits,market=preparation_namespace(observed(liquidity_usd=0))
    ns.update(lista_pares=queue,INCOMPLETE_RETRIES=3,random=SimpleNamespace(uniform=lambda *a:1.),
        warn_if_nulls=Mock(),_candidate_age_minutes=lambda token:3,
        log=SimpleNamespace(info=Mock(),debug=Mock()),_store_policy_reject=Mock(),_research_decision=Mock(),
        _remember_stream_candidate_cooldown=Mock(),_PUMPFUN_STREAM_COOLDOWN_NO_LIQ_S=30,
        _remove_from_queue_if_present=queue.eliminar_par,
        _requeue_with_stats=lambda addr,token=None,**kw:queue.requeue(addr,**kw))
    return ns,waits,market


@pytest.mark.parametrize('wait',WAITS)
@pytest.mark.parametrize('charged',['no_liq','mcap_low','other',''])
def test_total_attempts_remain_observable_without_spending_preserved_budget(environment,wait,charged):
    waited(environment,wait)
    meta=deepcopy(queue.meta(MINT));assert meta['budgeted_attempts']==0
    assert queue.requeue(MINT,reason=charged,backoff=7)
    assert queue.meta(MINT)['attempts']==13 and queue.meta(MINT)['budgeted_attempts']==1
    assert queue.requeue(MINT,reason=wait,backoff=9)
    row=queue.snapshot()[0]
    assert row['attempts']==14 and row['budgeted_attempts']==1 and row['retries_left']==4
    assert row['first_seen']==meta['first_seen'] and row['next_try']==environment[0]+9


@pytest.mark.parametrize('wait',WAITS)
@pytest.mark.parametrize('metric',['too_young','mcap_low','vol_low','liq_low'])
def test_native_policy_limit_is_unchanged_with_uncertainty_between_charged_retries(environment,wait,metric):
    waited(environment,wait,retries=100)
    token={'address':MINT,'age_minutes':3,'market_cap_usd':50000,'volume_24h_usd':10000,'liquidity_usd':10000}
    token[{'too_young':'age_minutes','mcap_low':'market_cap_usd','vol_low':'volume_24h_usd','liq_low':'liquidity_usd'}[metric]]=0.1
    ns=basic_native_branch();limit=requeue_policy._RULES_MAP[metric]['max_attempts']
    for i in range(limit):
        ns['check'](token,MINT)
        assert queue.meta(MINT)['budgeted_attempts']==i+1
        assert queue.meta(MINT)['retries']==100-i-1
        assert queue.requeue(MINT,reason=wait,backoff=1)
    ns['check'](token,MINT)
    assert queue.meta(MINT) is None and MINT in queue._processed
    assert ns['_stats']['filtered_out']==1
    assert ns['_store_policy_reject'].call_count==1


@pytest.mark.asyncio
@pytest.mark.parametrize('wait',WAITS)
async def test_native_zero_liquidity_still_expires_after_original_charged_attempt_limit(environment,wait):
    waited(environment,wait)
    ns,_,_=zero_liquidity_native(environment)
    token={'address':MINT,'discovered_via':'dex','created_at':dt.datetime.fromtimestamp(environment[0]-180,dt.timezone.utc)}
    for i in range(3):
        assert await ns['prepare'](token,SimpleNamespace(scalar=AsyncMock(return_value=None))) is None
        assert queue.meta(MINT)['budgeted_attempts']==i+1
        assert queue.requeue(MINT,reason=wait,backoff=1)
    await ns['prepare'](token,SimpleNamespace(scalar=AsyncMock(return_value=None)))
    assert queue.meta(MINT) is None and MINT in queue._processed


@pytest.mark.parametrize('preserved',[False,True])
def test_legacy_metadata_counter_is_not_invented_as_zero(environment,preserved):
    queue._pair_watch[MINT]={'retries':5,'attempts':3,'first_seen':environment[0],'next_try':environment[0]}
    assert queue.snapshot()[0]['budgeted_attempts']==3
    assert queue.requeue(MINT,reason=WAITS[0] if preserved else 'other',backoff=1)
    assert queue.meta(MINT)['budgeted_attempts']==3+int(not preserved)
    assert queue.meta(MINT)['attempts']==4


@pytest.mark.parametrize('wait',WAITS)
def test_absolute_residence_and_original_expiry_classification_are_not_reset(environment,wait):
    waited(environment,wait)
    first=queue.meta(MINT)['first_seen'];environment[0]=first+601
    assert queue.obtener_pares()==[] and queue.meta(MINT) is None
    assert (MINT in queue._processed) is (not wait.startswith('entry_observation:'))
    assert queue.log_queue_drop.call_args.kwargs['reason']=='incomplete_timeout'


def test_global_retry_limit_still_drops_and_persists_real_rejections(environment):
    assert queue.agregar_si_nuevo(MINT,retries=2)
    for _ in range(2):assert queue.requeue(MINT,reason='other',backoff=1)
    assert queue.meta(MINT) is None and MINT in queue._processed
    queue._persist.assert_called_once_with(MINT)
    assert queue.log_queue_drop.call_args.kwargs['reason']=='retries_exhausted'


@pytest.mark.asyncio
@pytest.mark.parametrize('uncertainty',['age','snapshot'])
@pytest.mark.parametrize('resolved_liquidity',[0,10000])
async def test_actual_common_prefix_and_defer_recover_with_original_queue_lifetime(environment,uncertainty,resolved_liquidity):
    assert queue.agregar_si_nuevo(MINT)
    first=queue.meta(MINT)['first_seen']
    ns,_,market=zero_liquidity_native(environment)
    ns.update(time=queue.time,_pending_ai_vectors={MINT:object()},_queue_add_if_new=Mock(),
        requeue=queue.requeue,
        BIRTH_CLOCK_FIELDS=BIRTH_CLOCK_FIELDS,BIRTH_FIELD=BIRTH_FIELD,merge_birth_context=merge_birth_context)
    ns['_stats']['requeues']=0
    ns['_candidate_age_minutes']=compute_age_minutes
    nodes=[run_function(name) for name in ('_remember_queue_context','_requeue_with_stats',
        '_ensure_requeue_with_stats','_defer_entry_observation')]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'run_bot.py','exec'),ns)
    token={'address':MINT,'discovered_via':'dex'}
    for _ in range(12):
        market.return_value=None if uncertainty=='snapshot' else observed()
        assert await ns['prepare'](token,SimpleNamespace(scalar=AsyncMock(return_value=None))) is None
        environment[0]+=6
    assert queue.meta(MINT)['attempts']==12 and queue.meta(MINT)['budgeted_attempts']==0
    assert queue.meta(MINT)['retries']==5 and queue.meta(MINT)['first_seen']==first
    assert MINT not in ns['_pending_ai_vectors']
    assert all(call.kwargs['action']=='wait' for call in ns['_research_decision'].call_args_list)
    ns['_store_policy_reject'].assert_not_called()
    market.return_value=observed(liquidity_usd=resolved_liquidity)
    token['created_at']=dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=3)
    result=await ns['prepare'](token,SimpleNamespace(scalar=AsyncMock(return_value=None)))
    assert queue.meta(MINT)['first_seen']==first
    assert queue.meta(MINT)['budgeted_attempts']==int(resolved_liquidity==0)
    if resolved_liquidity==0:assert result is None and queue.retries_left(MINT)==4
    else:assert result is not None and queue.retries_left(MINT)==5
