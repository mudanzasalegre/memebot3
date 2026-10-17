"""Existing shadow price observations feed scheduling, never outcomes or orders."""
from __future__ import annotations

from copy import deepcopy
import datetime as dt
import json
import time
from types import SimpleNamespace

import pytest

from features.token_clock_semantics import PROOF_COLUMN, capture_clock_proof
import runtime.hot_queue as hot
from runtime.hot_queue import HotQueue
from runtime.shadow_observation_feed import shadow_market_candidate
from test_shadow_state_recovery import native_namespace
from utils.market_observation import stamp_market_observation

MINT = 'So11111111111111111111111111111111111111112'


def original_vector(now):
    captured = now - dt.timedelta(minutes=1)
    token = {'address': MINT, 'created_at': now - dt.timedelta(minutes=3)}
    row = {'address': MINT, 'timestamp': captured, 'age_minutes': 2., 'queue_age_minutes': None,
        'entry_regime': 'pump_early', 'price_usd': 1., 'liquidity_usd': 100.,
        'price_pct_5m': 0., 'score_total': 99., 'label': 1, 'target_total_pnl_pct': 1000000.}
    row[PROOF_COLUMN] = capture_clock_proof(row, token)
    return row


def market(**changes):
    return stamp_market_observation({'address': MINT, 'price_usd': 2.,
        'liquidity_usd': 50000., 'market_cap_usd': 100000., 'txns_last_5m': 1000.,
        'price_pct_5m': 300., **changes}, 'dexscreener', received_at=time.time() - .001)


async def monitored_shadow(tmp_path, monkeypatch, *, paused=False, enabled=True):
    ns, _, _, _ = native_namespace(tmp_path, monkeypatch)
    now = dt.datetime.now(dt.timezone.utc)
    ns['utc_now'] = lambda: now
    row = original_vector(now)
    monkeypatch.setattr(hot, 'runner_priority_generation', lambda: 'checked-family')
    monkeypatch.setattr(hot, 'learned_runner_priorities', lambda rows, **kw: [{'bonus': 0.} for _ in rows])
    monkeypatch.setattr(hot, 'candidate_priority_score', lambda *a, **kw: 80.)
    queue = HotQueue(persist_events=False)
    ns.update(GLOBAL_HOT_QUEUE=queue, CFG=SimpleNamespace(HOT_QUEUE_ENABLED=enabled),
        _runtime_discovery_paused=paused)
    ns['price_service'].get_price.return_value = market()
    # Native feature builders transport the original proof as canonical JSON,
    # which is also the retained Parquet string-column representation.
    transport = {**row, PROOF_COLUMN:json.dumps(row[PROOF_COLUMN], sort_keys=True)}
    assert await ns['_open_shadow'](MINT, transport, price_hint=1., force=True, regime='pump_early')
    return ns, queue, row


@pytest.mark.asyncio
async def test_native_shadow_tick_offers_current_observation_to_scanner_without_another_lookup(tmp_path, monkeypatch):
    ns, queue, original = await monitored_shadow(tmp_path, monkeypatch)
    before = deepcopy(ns['_shadow_positions'][MINT]['vec'])
    await ns['_tick_shadows']()
    assert ns['price_service'].get_price.await_count == 1
    assert ns['price_service'].get_price_usd.await_count == 0
    assert queue.snapshot()['size'] == 1, 'Current observed shadow updates never reached the scanner'
    candidate = queue.pop_batch(1)[0]
    assert candidate['address'] == MINT and candidate['price_usd'] == 2.
    assert candidate['price_pct_5m'] == 300. and candidate['liquidity_usd'] == 50000.
    assert candidate['created_at'] == original[PROOF_COLUMN]['inputs']['created_at']
    assert candidate['discovered_via'] == candidate['source'] == 'shadow_followup'
    assert 'label' not in candidate and 'target_total_pnl_pct' not in candidate and 'score_total' not in candidate
    assert ns['_shadow_positions'][MINT]['vec'] == before


@pytest.mark.asyncio
@pytest.mark.parametrize('paused,enabled', [(True, True), (False, False)])
async def test_shadow_monitor_continues_but_scanner_admission_respects_pause_and_disable(tmp_path, monkeypatch, paused, enabled):
    ns, queue, _ = await monitored_shadow(tmp_path, monkeypatch, paused=paused, enabled=enabled)
    await ns['_tick_shadows']()
    assert ns['price_service'].get_price.await_count == 1
    assert queue.snapshot()['size'] == 0


def state():
    now = dt.datetime.now(dt.timezone.utc)
    return {'opened_at': now - dt.timedelta(seconds=30), 'vec': original_vector(now),
        'highest_pnl_pct': 1e9, 'max_pnl_pct_seen': 1e9, 'partial_taken': True,
        'realized_pnl_ratio': 1e9}


@pytest.mark.parametrize('invalid', ['address', 'state', 'opening', 'closed', 'vector', 'proof',
    'proof_digest', 'row_address', 'future_open', 'open_before_capture', 'measured_only'])
def test_unavailable_or_invalid_original_identity_and_clocks_do_not_rejuvenate(invalid):
    saved, address = state(), MINT
    if invalid == 'address': address = 'not-a-mint'
    elif invalid == 'state': saved = None
    elif invalid == 'opening': saved['opening_pending'] = True
    elif invalid == 'closed': saved['close_observation'] = None
    elif invalid == 'vector': saved['vec'] = None
    elif invalid == 'proof': saved['vec'].pop(PROOF_COLUMN)
    elif invalid == 'proof_digest': saved['vec'][PROOF_COLUMN]['payload_sha256'] = '0'*64
    elif invalid == 'row_address': saved['vec']['address'] = 'not-the-original'
    elif invalid == 'future_open': saved['opened_at'] += dt.timedelta(hours=1)
    elif invalid == 'open_before_capture': saved['opened_at'] -= dt.timedelta(hours=1)
    else:
        saved['vec'].pop(PROOF_COLUMN)
        saved['vec']['created_at'] = None
    before = deepcopy(saved)
    assert shadow_market_candidate(address, saved, market()) is None
    assert saved == before


@pytest.mark.parametrize('invalid', ['absent', 'wrong_address', 'no_receipt', 'wrong_receipt_address',
    'stale', 'future', 'changed_value', 'boolean', 'zero', 'nan'])
def test_only_current_address_bound_positive_price_can_supply_scanner_input(invalid):
    tick = market()
    if invalid == 'absent': tick = None
    elif invalid == 'wrong_address': tick['address'] = 'different'
    elif invalid == 'no_receipt': tick.pop('market_observation')
    elif invalid == 'wrong_receipt_address': tick['market_observation']['address'] = 'different'
    elif invalid in {'stale', 'future'}:
        tick['market_observation']['fields']['price_usd']['received_at'] += -31 if invalid == 'stale' else 3600
    elif invalid == 'changed_value': tick['price_usd'] = 99.
    else: tick = market(price_usd={'boolean':True, 'zero':0., 'nan':float('nan')}[invalid])
    assert shadow_market_candidate(MINT, state(), tick) is None


@pytest.mark.parametrize('liquidity', [None, 0., 50000.])
def test_no_shadow_outcome_old_feature_or_prediction_is_reused(liquidity):
    saved = state()
    before = deepcopy(saved)
    tick = market(liquidity_usd=liquidity, txns_last_5m=None, price_pct_5m=None,
        label=1, pnl_pct=1e9, score_total=100, learned_runner_priority={'bonus':99})
    original_market = deepcopy(tick)
    candidate = shadow_market_candidate(MINT, saved, tick)
    assert candidate is not None and candidate['liquidity_usd'] == liquidity
    assert candidate['price_pct_5m'] is None and candidate['txns_last_5m'] is None
    assert candidate['created_at'] == saved['vec'][PROOF_COLUMN]['inputs']['created_at']
    forbidden = {'age_minutes', 'highest_pnl_pct', 'max_pnl_pct_seen', 'partial_taken',
        'realized_pnl_ratio', 'label', 'pnl_pct', 'score_total', 'target_total_pnl_pct',
        'learned_runner_priority', PROOF_COLUMN, 'entry_lane', 'paper_entry_policy'}
    assert not forbidden.intersection(candidate)
    candidate['market_observation']['fields']['price_usd']['value'] = 99
    assert saved == before and tick == original_market


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['queue', 'bridge'])
async def test_optional_queue_or_bridge_failure_does_not_interrupt_native_shadow_close(tmp_path, monkeypatch, fault):
    ns, _, _ = await monitored_shadow(tmp_path, monkeypatch)
    def fail(*args, **kwargs): raise RuntimeError('synthetic optional queue fault')
    if fault == 'queue': ns['GLOBAL_HOT_QUEUE'] = SimpleNamespace(add=fail)
    else:
        import runtime.shadow_observation_feed as bridge
        monkeypatch.setattr(bridge, 'shadow_market_candidate', fail)
    ns['exit_policy'].should_exit = lambda *args, **kwargs: 'TIMEOUT'
    await ns['_tick_shadows']()
    assert ns['_stats']['shadow_feed_failed'] == 1
    assert ns['_stats']['appended_shadow'] == 1 and not ns['_shadow_positions']
    events = [json.loads(line) for line in ns['research_runtime'].RESEARCH_EVENTS_PATH.read_text().splitlines()]
    outcomes = [row for row in events if row['event_type'] == 'candidate_outcome']
    assert len(outcomes) == 1 and outcomes[0]['exit_reason'] == 'TIMEOUT'
    assert ns['price_service'].get_price.await_count == 1


@pytest.mark.asyncio
async def test_restored_original_shadow_can_supply_new_observation_without_backfill(tmp_path, monkeypatch):
    from runtime import shadow_state
    ns, queue, _ = await monitored_shadow(tmp_path, monkeypatch)
    original = deepcopy(ns['_shadow_positions'][MINT])
    ns['_shadow_positions'].clear()
    restored = shadow_state.restore(tmp_path, research=ns['research_runtime'])
    ns['_shadow_positions'].update(restored['positions'])
    assert ns['_shadow_positions'][MINT]['vec'] == original['vec']
    await ns['_tick_shadows']()
    assert queue.snapshot()['size'] == 1
    assert queue.pop_batch(1)[0]['created_at'] == json.loads(original['vec'][PROOF_COLUMN])['inputs']['created_at']
    assert ns['price_service'].get_price.await_count == 1


@pytest.mark.asyncio
async def test_identical_observations_are_deduplicated_and_changes_keep_native_recheck_interval(tmp_path, monkeypatch):
    ns, queue, _ = await monitored_shadow(tmp_path, monkeypatch)
    clock = [time.time()]
    monkeypatch.setattr(queue, '_now', lambda: clock[0])
    await ns['_tick_shadows']()
    assert len(queue.pop_batch(1)) == 1
    clock[0] += 16
    await ns['_tick_shadows']()
    assert queue.pop_batch(1) == []
    ns['price_service'].get_price.return_value = market(price_pct_5m=1000.)
    await ns['_tick_shadows']()
    assert len(queue.pop_batch(1)) == 1
    ns['price_service'].get_price.return_value = market(price_pct_5m=2000.)
    clock[0] += 1
    await ns['_tick_shadows']()
    assert queue.pop_batch(1) == []
    assert ns['price_service'].get_price.await_count == 4


def test_valid_measured_age_without_original_birth_clock_cannot_become_new_birth():
    now = dt.datetime.now(dt.timezone.utc)
    row = original_vector(now)
    row[PROOF_COLUMN] = capture_clock_proof(row, {'address':MINT, 'age_minutes':2.})
    assert shadow_market_candidate(MINT, {'opened_at':now, 'vec':row}, market()) is None


def test_original_typed_birth_receipt_is_retained_detached_not_replaced_by_current_listing():
    from analytics.token_birth import FIELD
    now = dt.datetime.now(dt.timezone.utc)
    row = original_vector(now)
    born = now - dt.timedelta(minutes=3)
    receipt = {'version':'birdeye_mint_creation_receipt_v1', 'source':'birdeye', 'chain':'solana',
        'address':MINT, 'created_at':born.isoformat(), 'slot':123, 'tx_hash':'1'*64,
        'received_at':(now-dt.timedelta(seconds=65)).timestamp(),
        'basis':'provider_reported_mint_creation_not_independent_chain_verification'}
    row[PROOF_COLUMN] = capture_clock_proof(row, {'address':MINT, 'created_at':born, FIELD:receipt})
    saved = {'opened_at':now-dt.timedelta(seconds=30), 'vec':row}
    before = deepcopy(saved)
    candidate = shadow_market_candidate(MINT, saved, market(pairCreatedAt=now.isoformat()))
    assert candidate is not None and candidate[FIELD] == receipt
    assert candidate['created_at'] == born.isoformat()
    candidate[FIELD]['slot'] = 999
    assert saved == before and receipt['slot'] == 123
