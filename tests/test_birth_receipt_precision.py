"""Original numeric decision-clock precision, never tolerance or future repair."""
import datetime as dt
import math

import pytest

from analytics.token_birth import checked_token_birth, merge_birth_context

MINT = 'So11111111111111111111111111111111111111112'
ANCHOR = dt.datetime(2026,10,13,10,0,0,123456,tzinfo=dt.timezone.utc)


def receipt(received):
    born = ANCHOR - dt.timedelta(minutes=3)
    return {'address':MINT,'created_at':born,'token_birth_observation':{
        'version':'birdeye_mint_creation_receipt_v1','source':'birdeye','chain':'solana',
        'address':MINT,'created_at':born.isoformat(),'slot':42,'tx_hash':'1'*64,
        'received_at':received,'basis':'provider_reported_mint_creation_not_independent_chain_verification'}}


def test_original_numeric_now_does_not_reject_equal_receipt_after_datetime_round_down():
    now = math.nextafter(ANCHOR.timestamp(),math.inf)
    assert dt.datetime.fromtimestamp(now,dt.timezone.utc).timestamp() < now
    raw = receipt(now)
    assert checked_token_birth(raw['token_birth_observation'],MINT,now=now) == raw['token_birth_observation']


def test_merge_preserves_original_numeric_now_for_nested_birth_receipt_validation():
    now = math.nextafter(ANCHOR.timestamp(),math.inf)
    raw = receipt(now)
    result = merge_birth_context({'address':MINT},raw,now=now)
    assert result['token_birth_observation'] == raw['token_birth_observation']


@pytest.mark.parametrize('direction',[math.inf,-math.inf])
def test_original_numeric_limit_rejects_strictly_future_receipt_even_if_rounding_agrees(direction):
    now = math.nextafter(ANCHOR.timestamp(),direction)
    raw = receipt(math.nextafter(now,math.inf))
    assert checked_token_birth(raw['token_birth_observation'],MINT,now=now) is None
    assert merge_birth_context({'address':MINT},raw,now=now) == {'address':MINT}


@pytest.mark.parametrize('now',[ANCHOR,ANCHOR.isoformat(),ANCHOR.timestamp()*1000])
def test_datetime_string_and_documented_millisecond_normalization_remain_exact(now):
    raw = receipt(ANCHOR.timestamp())
    assert checked_token_birth(raw['token_birth_observation'],MINT,now=now) == raw['token_birth_observation']


@pytest.mark.parametrize('now',[True,None,float('nan'),float('inf'),-1,'invalid'])
def test_invalid_explicit_now_is_not_fabricated(now):
    raw = receipt(ANCHOR.timestamp())
    if now is None:
        # Default now is allowed, not an explicit absent-source decision time.
        assert checked_token_birth(raw['token_birth_observation'],MINT) == raw['token_birth_observation']
        return
    assert checked_token_birth(raw['token_birth_observation'],MINT,now=now) is None
