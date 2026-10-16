"""Strict admission versus downstream observation, without wall-clock luck."""
import asyncio
import math
from types import SimpleNamespace
import time
import threading

import pytest

import test_birdeye_throttle as native
from test_birdeye_throttle import isolated
from fetcher import birdeye as be


def delayed_session_clock(monkeypatch, *, cancel_first_wait):
    now=[100.]
    native_wait=be._throttle_wait
    admissions=[]
    def wait():
        with be._lock:
            delay=native_wait()
            if delay==0:
                admissions.append(be._last_call_ts)
            return delay
    monkeypatch.setattr(be,'_throttle_wait',wait)
    monkeypatch.setattr(be,'time',SimpleNamespace(monotonic=lambda:now[0],time=time.time))
    monkeypatch.setattr(native,'time',SimpleNamespace(monotonic=lambda:now[0],time=time.time))
    waits=[]
    async def sleep(delay):
        waits.append(delay)
        if cancel_first_wait and len(waits)==1:
            await asyncio.Future()  # Genuine cancellation, no consumed slot.
        now[0]=math.nextafter(now[0]+delay,math.inf)
        await asyncio.sleep(0)
    monkeypatch.setattr(be,'asyncio',SimpleNamespace(sleep=sleep,CancelledError=asyncio.CancelledError))
    original_install=native.install
    all_calls=[]
    def install(mp,response):
        calls=original_install(mp,response)
        all_calls.append(calls)
        factory=be.aiohttp.ClientSession
        created=[0]
        class Session:
            def __init__(self,*args,**kwargs):
                self.inner=factory(*args,**kwargs)
                created[0]+=1
                self.number=created[0]
            async def __aenter__(self):
                value=await self.inner.__aenter__()
                if self.number==1:
                    now[0]+=be._MIN_INTERVAL*.3
                return value
            async def __aexit__(self,*args):return await self.inner.__aexit__(*args)
        mp.setattr(be.aiohttp,'ClientSession',Session)
        return calls
    monkeypatch.setattr(native,'install',install)
    return admissions,all_calls,waits


@pytest.mark.parametrize('case',[
    'test_native_http_shared_pacing_survives_new_event_loops',
    'test_three_resources_share_one_global_admission',
    'test_cancelled_native_request_wait_releases_ownership',
])
def test_native_pacing_cases_measure_admission_not_late_session_observation(monkeypatch,case):
    admissions,all_calls,waits=delayed_session_clock(monkeypatch,
        cancel_first_wait=case=='test_cancelled_native_request_wait_releases_ownership')
    target=getattr(native,case)
    if asyncio.iscoroutinefunction(target):asyncio.run(target(monkeypatch))
    else:target(monkeypatch)
    native.assert_spaced(admissions)  # Strict: no epsilon/tolerance/looser RPM.
    calls=all_calls[0]
    assert len(admissions)==len(calls)
    assert calls[1][0]-calls[0][0]<be._MIN_INTERVAL
    assert waits


def paused_thread_observer(monkeypatch):
    receipts=[]
    observations=[]
    original_wait=be._throttle_wait
    def wait():
        with be._lock:
            delay=original_wait()
            if delay==0:receipts.append(be._last_call_ts)
            return delay
    monkeypatch.setattr(be,'_throttle_wait',wait)
    original_throttle=be._throttle
    state_lock=threading.Lock()
    released=threading.Event()
    count=[0]
    async def throttle():
        await original_throttle()
        with state_lock:
            count[0]+=1
            first=count[0]==1
            if count[0]>=13:released.set()
        if first:
            assert released.wait(5),'Other native threads must make progress while this observer is paused'
    monkeypatch.setattr(be,'_throttle',throttle)
    original_cycle=native.cycle
    async def cycle(*args,**kwargs):
        values=await original_cycle(*args,**kwargs)
        observations.extend(values)
        return values
    monkeypatch.setattr(native,'cycle',cycle)
    return receipts,observations


def test_native_thread_case_does_not_confuse_a_later_shared_value_with_its_own_receipt(monkeypatch):
    receipts,observations=paused_thread_observer(monkeypatch)
    native.test_global_spacing_across_four_threads_and_event_loops(monkeypatch)
    native.assert_spaced(receipts)
    assert len(receipts)==len(observations)==16
    assert len(set(observations))<len(observations)


def test_admission_recorder_still_rejects_a_genuine_early_admission(monkeypatch):
    stamps=iter([100.,100.+be._MIN_INTERVAL/2])
    def broken_wait():
        be._last_call_ts=next(stamps)
        return 0.
    monkeypatch.setattr(be,'_throttle_wait',broken_wait)
    receipts=native.capture_admissions(monkeypatch)
    async def run():
        await be._throttle()
        await be._throttle()
    asyncio.run(run())
    assert len(receipts)==2
    with pytest.raises(AssertionError):native.assert_spaced(receipts)


def test_recorder_counts_only_actual_native_admission_not_a_wait(monkeypatch):
    clock=[100.]
    monkeypatch.setattr(be,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    receipts=native.capture_admissions(monkeypatch)
    assert be._throttle_wait()==0.
    clock[0]=100.+be._MIN_INTERVAL/2
    assert be._throttle_wait()>0.
    assert receipts==[100.]
    clock[0]=math.nextafter(100.+be._MIN_INTERVAL,math.inf)
    assert be._throttle_wait()==0.
    assert len(receipts)==2
    native.assert_spaced(receipts)
