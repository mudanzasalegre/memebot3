"""Untrusted creation references must survive discovery without becoming age."""
from copy import deepcopy

import pytest

from fetcher import pumpfun
from runtime.entry_observation import discovery_candidate, prepare_entry_candidate
from utils.market_observation import stamp_market_observation

MINT = 'So11111111111111111111111111111111111111112'
SIGNATURE = '1' * 64  # Syntactic reference only; never a verified transaction.
FIELD = 'mint_creation_reference'


def test_native_parser_preserves_creation_reference_without_fabricating_birth():
    result = pumpfun._parse_event({'mint': MINT, 'signature': SIGNATURE, 'txType': 'create', 'slot': 123})
    assert result['address'] == MINT and result['created_at'] is None
    assert result['age_minutes'] is None and result['age_min'] is None
    assert result[FIELD]['tx_hash'] == SIGNATURE and result[FIELD]['slot'] == 123
    assert result[FIELD]['address'] == MINT
    assert result[FIELD]['received_at'] == result['discovered_at'].timestamp()
    assert 'token_birth_observation' not in result


@pytest.mark.parametrize('bad', [None, '', 'invalid', 1, True, '0'*88])
def test_invalid_signature_never_becomes_a_reference_or_birth(bad):
    result = pumpfun._parse_event({'mint': MINT, 'signature': bad, 'txType': 'create'})
    assert FIELD not in result and result['created_at'] is None


def test_native_entry_detaches_reference_and_market_cannot_replace_it():
    queued = pumpfun._parse_event({'mint': MINT, 'signature': SIGNATURE, 'txType': 'create'})
    assert FIELD in queued
    expected = deepcopy(queued[FIELD])
    detached = discovery_candidate(queued)
    assert detached[FIELD] == expected
    detached[FIELD]['tx_hash'] = 'changed'
    assert queued[FIELD] == expected
    snapshot = stamp_market_observation({'address': MINT, 'price_usd': 1., FIELD: {'tx_hash':'injected'}}, 'dexscreener')
    ready = prepare_entry_candidate(queued, snapshot)
    assert ready[FIELD] == expected and ready['created_at'] is None


@pytest.mark.parametrize('update', [
    {'txType':'buy'}, {'signature':SIGNATURE,'txHash':'2'*88}, {'slot':True},
    {'slot':-1}, {'slot':'123'}, {'data':{'signature':'2'*88}},
])
def test_non_creation_or_ambiguous_reference_is_not_promoted(update):
    result = pumpfun._parse_event({'mint':MINT,'signature':SIGNATURE,'txType':'create'} | update)
    assert FIELD not in result and result['created_at'] is None


@pytest.mark.parametrize('bad', [True,float('nan'),float('inf'),0,-1,'100'])
def test_reference_receipt_clock_cannot_be_faked(bad):
    from analytics.mint_creation_reference import checked_reference
    result = pumpfun._parse_event({'mint':MINT,'signature':SIGNATURE,'txType':'create'})
    raw = result[FIELD] | {'received_at':bad}
    assert checked_reference(raw,MINT) is None


def test_repeated_reference_keeps_original_receipt_and_existing_birth():
    from analytics.mint_creation_reference import merge_reference
    first = pumpfun._parse_event({'mint':MINT,'signature':SIGNATURE,'txType':'create'})
    newer = deepcopy(first)
    newer[FIELD]['received_at'] += 1
    assert merge_reference(first,newer,now=newer[FIELD]['received_at'])[FIELD] == first[FIELD]
    newer['address'] = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
    assert merge_reference(first,newer)[FIELD] == first[FIELD]
