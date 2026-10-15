"""Deterministic precision/causality QA, no runtime/provider or sleeping clock SLA."""
import asyncio
import datetime as dt
import math

import pytest

from runtime import entry_clock as clock
ANCHOR = dt.datetime(2026,10,15,12,35,6,90880,tzinfo=dt.timezone.utc)
RECEIVED = 1792067706.0908809
assert ANCHOR.timestamp() < RECEIVED


@pytest.mark.asyncio
async def test_observed_floor_catches_up_without_receipt_or_time_repair():
    next_utc = ANCHOR+dt.timedelta(microseconds=1)
    reads = iter([ANCHOR,next_utc])
    sleeps = []
    async def sleep(seconds): sleeps.append(seconds)
    result = await clock.capture_causal_utc(minimum_epoch=RECEIVED,utc=lambda:next(reads),
        wall=lambda:RECEIVED+1,monotonic=lambda:10.,sleep=sleep)
    assert result is next_utc and sleeps == [.001]
    assert ANCHOR.timestamp() < RECEIVED <= result.timestamp()


@pytest.mark.asyncio
async def test_strictly_future_receipt_cannot_become_valid_by_waiting_for_it():
    seen = []
    async def forbidden(seconds): raise AssertionError('Must not wait for a future receipt')
    result = await clock.capture_causal_utc(minimum_epoch=RECEIVED,
        utc=lambda:seen.append(True),wall=lambda:math.nextafter(RECEIVED,-math.inf),
        monotonic=lambda:10.,sleep=forbidden)
    assert result is None and not seen


@pytest.mark.asyncio
async def test_reads_are_bounded_even_if_monotonic_does_not_advance_in_fixture():
    seen = []
    async def sleep(seconds): seen.append(seconds)
    result = await clock.capture_causal_utc(minimum_epoch=RECEIVED,utc=lambda:ANCHOR,
        wall=lambda:RECEIVED+1,monotonic=lambda:10.,sleep=sleep,max_reads=3)
    assert result is None and len(seen) == 3


@pytest.mark.asyncio
async def test_deadline_cannot_certify_a_late_observed_read():
    moments = iter([10.,10.,10.2])
    reads = iter([ANCHOR,ANCHOR+dt.timedelta(seconds=1)])
    async def sleep(seconds): pass
    result = await clock.capture_causal_utc(minimum_epoch=RECEIVED,utc=lambda:next(reads),
        wall=lambda:RECEIVED+2,monotonic=lambda:next(moments),sleep=sleep)
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[None,True,0,-1,float('nan'),float('inf'),'invalid'])
async def test_invalid_wall_is_unknown_not_a_causal_time(bad):
    result = await clock.capture_causal_utc(minimum_epoch=RECEIVED,utc=lambda:ANCHOR,
        wall=lambda:bad,monotonic=lambda:10.)
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[True,0,-1,float('nan'),float('inf'),'invalid'])
async def test_invalid_receipt_clock_is_never_coerced(bad):
    assert await clock.capture_causal_utc(minimum_epoch=bad) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[None,True,10.,'invalid',ANCHOR.replace(tzinfo=None)])
async def test_invalid_or_naive_utc_is_not_accepted(bad):
    assert await clock.capture_causal_utc(utc=lambda:bad,wall=lambda:RECEIVED+1,monotonic=lambda:10.) is None


@pytest.mark.asyncio
async def test_future_utc_read_is_not_clamped_to_numeric_wall():
    assert await clock.capture_causal_utc(utc=lambda:ANCHOR+dt.timedelta(seconds=5),
        wall=lambda:RECEIVED,monotonic=lambda:10.) is None


@pytest.mark.asyncio
async def test_cancellation_propagates_while_waiting_for_a_real_tick():
    entered = asyncio.Event()
    async def sleep(seconds): entered.set(); await asyncio.Event().wait()
    task = asyncio.create_task(clock.capture_causal_utc(minimum_epoch=RECEIVED,utc=lambda:ANCHOR,
        wall=lambda:RECEIVED+1,monotonic=lambda:10.,sleep=sleep))
    await entered.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task


@pytest.mark.asyncio
async def test_backwards_numeric_wall_cannot_be_certified_after_wait():
    walls = iter([RECEIVED+2,RECEIVED+1])
    assert await clock.capture_causal_utc(utc=lambda:ANCHOR,wall=lambda:next(walls),
        monotonic=lambda:10.) is None


@pytest.mark.asyncio
async def test_backwards_monotonic_cannot_be_certified():
    moments = iter([10.,9.])
    assert await clock.capture_causal_utc(utc=lambda:ANCHOR,wall=lambda:RECEIVED+1,
        monotonic=lambda:next(moments)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('options',[
    {'max_wait_s':True},{'max_wait_s':None},{'max_wait_s':-1},{'max_wait_s':2},
    {'max_wait_s':float('inf')},{'max_wait_s':10**1000},
    {'max_reads':True},{'max_reads':None},{'max_reads':0},{'max_reads':1.5},{'max_reads':1025}],
    ids=['bool-wait','missing-wait','negative-wait','excess-wait','infinite-wait','overflow-wait',
         'bool-reads','missing-reads','zero-reads','fractional-reads','excess-reads'])
async def test_invalid_bounds_do_not_observe_or_create_a_time(options):
    seen = []
    def forbidden(): seen.append(True); raise AssertionError('Invalid bounds must not observe')
    assert await clock.capture_causal_utc(utc=forbidden,wall=forbidden,monotonic=forbidden,**options) is None
    assert not seen


@pytest.mark.asyncio
async def test_real_observed_catchup_retains_strict_original_birth_validation():
    from analytics.token_birth import FIELD,VERSION,BASIS,checked_token_birth
    from features.builder import build_feature_vector
    from features.token_clock_semantics import PROOF_COLUMN
    mint = 'So11111111111111111111111111111111111111112'
    born = ANCHOR-dt.timedelta(minutes=2)
    receipt = {'version':VERSION,'basis':BASIS,'source':'birdeye','chain':'solana',
        'address':mint,'created_at':born.isoformat(),'slot':42,'tx_hash':'1'*64,'received_at':RECEIVED}
    token = {'address':mint,'created_at':born,FIELD:receipt}
    assert checked_token_birth(receipt,mint,now=ANCHOR) is None
    assert PROOF_COLUMN not in build_feature_vector(token,now=ANCHOR).attrs
    observed = ANCHOR+dt.timedelta(microseconds=1)
    reads = iter([ANCHOR,observed])
    async def sleep(seconds): pass
    captured = await clock.capture_causal_utc(minimum_epoch=RECEIVED,utc=lambda:next(reads),
        wall=lambda:RECEIVED+1,monotonic=lambda:10.,sleep=sleep)
    assert captured is observed
    current = build_feature_vector(token,now=captured)
    assert PROOF_COLUMN in current.attrs and current.timestamp is captured
    assert receipt['received_at'] == RECEIVED and token[FIELD] is receipt
