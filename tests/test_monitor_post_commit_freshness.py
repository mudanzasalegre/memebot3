"""Whole native monitor with controlled clocks and inert sell/session seams.

No operational LIVE/PAPER launch, signing, socket or financial-fill evidence.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from utils import market_observation
from utils.market_observation import fresh_market_value,liquidity_crushed,stamp_market_observation


MINT='A'*44


async def native_monitor_case(monkeypatch,*,initial_age=29.,delay=2.,commit_source='peak',
                              partial_taken=False,liquidity=180.,cancel_commit=False):
    import asyncio
    clock=[2_000_000_000.]
    monkeypatch.setattr(market_observation.time,'time',lambda:clock[0])
    price_tick=stamp_market_observation({'address':MINT,'price_usd':3.},'jupiter',
        received_at=clock[0]-initial_age)
    liquidity_tick=stamp_market_observation({'address':MINT,'liquidity_usd':liquidity},'dexscreener',
        received_at=clock[0])
    pos=SimpleNamespace(address=MINT,token_mint=MINT,entry_regime='pump_early',
        opened_at=dt.datetime.fromtimestamp(clock[0]-60,tz=dt.timezone.utc),
        price_source_at_buy='jupiter',buy_liquidity_usd=180.,buy_price_usd=1.,
        qty=100,partial_taken=partial_taken,highest_pnl_pct=200.,symbol='fixture',runner_exit_profile=None)
    calls={'sells':[],'exits':[],'commits':0,'rollbacks':0,'market_requests':[]}
    class Session:
        async def commit(self):
            calls['commits']+=1
            clock[0]+=delay
            if cancel_commit:raise asyncio.CancelledError
        async def rollback(self):calls['rollbacks']+=1
    async def positions(session):return [pos]
    async def prefetch(addresses):
        assert addresses==[MINT]
        return {MINT:price_tick}
    async def ensure(*args):return True
    async def get_price(address,**kwargs):
        assert address==MINT and kwargs=={'use_gt':True,'liquidity_only':True,'force_refresh':True}
        calls['market_requests'].append(kwargs)
        return liquidity_tick
    async def sell(position,quantity,**kwargs):
        calls['sells'].append({'quantity':quantity,**kwargs})
        return {'ok':False,'err':'owned inert fixture; no sell submitted'}
    async def should_exit(position,price,now,**kwargs):
        calls['exits'].append({'price':price,'now':now.isoformat(),**kwargs})
        return None
    quiet=lambda *args,**kwargs:None
    policy=SimpleNamespace(runner_exit_profile='fixture-profile' if commit_source=='policy' else None,
        liq_crush_fraction=.2,tp_partial_enabled=True)
    ns={'SessionLocal':Session,'Position':SimpleNamespace,'Optional':Optional,'Dict':dict,
        'dt':dt,'json':json,'DRY_RUN':False,'_wallet_sol_balance':0.,
        '_BUY_RECOVERY':SimpleNamespace(pending_addresses=set()),'_CLOSE_RECOVERY_PENDING':set(),
        '_load_open_positions':positions,'_prefetch_batch_prices':prefetch,
        '_ensure_position_entry_notional':ensure,'utc_now':lambda:dt.datetime.fromtimestamp(clock[0],tz=dt.timezone.utc),
        'parse_iso_utc':lambda value:dt.datetime.fromisoformat(value),'FORCE_JUP_IN_MONITOR':True,
        'price_service':SimpleNamespace(get_price=get_price),
        'fresh_market_value':fresh_market_value,'liquidity_crushed':liquidity_crushed,
        '_update_position_peak_metrics':lambda *args,**kwargs:commit_source=='peak',
        'runner_turbo_monitor':SimpleNamespace(observe_position=quiet),
        'exit_policy':SimpleNamespace(resolve_entry_regime=lambda pos:'pump_early',
            effective_exit_policy=lambda pos:policy,
            should_take_partial=lambda pos,pnl:not pos.partial_taken and pnl>=100,
            partial_ladder_plan=lambda *args:{'enabled':False},partial_sell_fraction=lambda *args:.5),
        'TP_PARTIAL_MIN_REMAIN_LAMPORTS':1,'_sell_position_guarded':sell,'_should_exit':should_exit,
        'strategy_runtime':SimpleNamespace(record_monitor_coverage=quiet,record_execution=quiet),
        'log_execution_event':quiet,'record_runtime_event':quiet,'_fmt':lambda value,*args:str(value),
        'log':SimpleNamespace(**{name:quiet for name in ('info','debug','warning','error','critical')})}
    source=ast.parse(Path('run_bot.py').read_text(encoding='utf-8'))
    function=next(n for n in source.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_check_positions')
    exec(compile(ast.Module(body=[function],type_ignores=[]),'<whole-native-monitor>','exec'),ns)
    if cancel_commit:
        with pytest.raises(asyncio.CancelledError):await ns['_check_positions'](Session())
    else:await ns['_check_positions'](Session())
    assert calls['commits']==1
    assert fresh_market_value(price_tick,'price_usd')==(3. if initial_age+delay<=30 else None)
    return calls,pos


@pytest.mark.asyncio
@pytest.mark.parametrize('commit_source',['peak','policy'])
@pytest.mark.parametrize('initial_age,delay',[(29.,2.),(0.,31.),(30.,.001)])
async def test_live_partial_does_not_use_a_price_expired_during_sql(monkeypatch,commit_source,initial_age,delay):
    calls,pos=await native_monitor_case(monkeypatch,commit_source=commit_source,
        initial_age=initial_age,delay=delay)
    assert calls['sells']==[]
    assert len(calls['exits'])==1 and calls['exits'][0]['price'] is None and calls['exits'][0]['pnl_pct'] is None
    assert pos.qty==100 and not pos.partial_taken


@pytest.mark.asyncio
@pytest.mark.parametrize('commit_source',['peak','policy'])
async def test_expired_price_cannot_suppress_a_fresh_zero_liquidity_crush(monkeypatch,commit_source):
    calls,pos=await native_monitor_case(monkeypatch,commit_source=commit_source,
        partial_taken=True,liquidity=0.)
    assert len(calls['sells'])==1 and calls['sells'][0]['reason']=='LIQUIDITY_CRUSH'
    assert calls['sells'][0]['quantity']==100 and calls['sells'][0]['price_hint'] is None
    assert calls['sells'][0]['price_source_hint'] is None
    assert not calls['exits'] and pos.qty==100


@pytest.mark.asyncio
@pytest.mark.parametrize('commit_source',['peak','policy'])
@pytest.mark.parametrize('initial_age,delay',[(0.,2.),(29.,1.),(30.,0.)])
async def test_fresh_and_exact_boundary_prices_preserve_native_partial(monkeypatch,commit_source,initial_age,delay):
    calls,pos=await native_monitor_case(monkeypatch,commit_source=commit_source,
        initial_age=initial_age,delay=delay)
    assert len(calls['sells'])==1 and calls['sells'][0]['reason']=='partial_fill'
    assert calls['sells'][0]['quantity']==50 and calls['sells'][0]['price_hint']==3.
    assert calls['sells'][0]['price_source_hint']=='jup_batch'
    assert calls['rollbacks']==1 and not calls['exits'] and pos.qty==100


@pytest.mark.asyncio
@pytest.mark.parametrize('commit_source',['peak','policy'])
async def test_valid_price_still_preserves_existing_post_partial_crush_override(monkeypatch,commit_source):
    calls,pos=await native_monitor_case(monkeypatch,commit_source=commit_source,
        initial_age=0.,partial_taken=True,liquidity=0.)
    assert not calls['sells']
    assert len(calls['exits'])==1 and calls['exits'][0]['price']==3. and calls['exits'][0]['liq_now']==0.
    assert pos.qty==100


@pytest.mark.asyncio
@pytest.mark.parametrize('commit_source',['peak','policy'])
async def test_cancelled_sql_cannot_submit_a_sell_or_fabricate_a_close(monkeypatch,commit_source):
    calls,pos=await native_monitor_case(monkeypatch,commit_source=commit_source,cancel_commit=True)
    assert not calls['sells'] and not calls['exits'] and pos.qty==100
