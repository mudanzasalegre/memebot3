"""Restart and IO-failure probes of native nonfinancial shadow tracking."""
from __future__ import annotations

import ast
import asyncio
import datetime as dt
import json
from copy import deepcopy
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def native_namespace(root, monkeypatch):
    from test_shadow_peak_observation import native_open
    from test_shadow_close_recovery import native_paths
    store, research = native_paths(root, monkeypatch)
    ns = native_open(root)
    now = ns['utc_now']()
    ns.update(PROJECT_ROOT=root, research_runtime=research, asyncio=asyncio,
        SimpleNamespace=SimpleNamespace, SLEEP_SECONDS=3, _RESEARCH_SHADOW_USE_GECKO=False,
        ML_POSITIVE_PNL_RATIO=.05, _stats={'appended_shadow': 0}, store_append=store.append,
        _seconds_from_opened_at=lambda opened, current: (current - opened).total_seconds(),
        _to_float=lambda value, *args: float(value) if value is not None else None,
        _note_runtime_error=Mock(), log=SimpleNamespace(info=Mock(), debug=Mock(), warning=Mock()),
        exit_policy=SimpleNamespace(effective_exit_policy=lambda state: SimpleNamespace(max_holding_h=1),
            should_take_partial=lambda *args: False, should_exit=lambda *args, **kwargs: None),
        price_service=SimpleNamespace(get_price=AsyncMock(return_value={'price_usd': 2.}),
            get_price_usd=AsyncMock(return_value=None)))
    tree = ast.parse(Path('run_bot.py').read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
        and node.name in {'_tick_shadows', '_shadow_monitor_loop'}]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'run_bot.py', 'exec'), ns)
    return ns, store, research, now


async def one_monitor_tick(ns, monkeypatch):
    from runtime import loop_scheduler
    async def poll(**kwargs):
        assert kwargs['ready'].is_set()
        await kwargs['tick']()
    monkeypatch.setattr(loop_scheduler, 'poll_discovery', poll)
    ready = asyncio.Event()
    ready.set()
    await ns['_shadow_monitor_loop'](ready)


@pytest.mark.asyncio
@pytest.mark.parametrize('price', [None, 1.])
async def test_native_monitor_recovers_open_original_state_after_memory_loss(tmp_path, monkeypatch, price):
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    ns['price_service'].get_price.return_value = {'price_usd': price}
    vector = {'address': 'synthetic-restart', 'timestamp': now - dt.timedelta(minutes=5),
        'entry_lane': 'pump_early_sniper_research', 'score_total': 73., 'nullable_input': None}
    await ns['_open_shadow'](vector['address'], vector, price_hint=price, force=True,
        regime='pump_early', reason='original')
    original = ns['_shadow_positions'][vector['address']]
    identity = original['shadow_instance_id']
    ns['_shadow_positions'].clear()
    research._OPEN_SHADOWS.clear()
    async def inspect():
        assert vector['address'] in ns['_shadow_positions'], 'Original open state was lost across restart'
        restored = ns['_shadow_positions'][vector['address']]
        assert restored['shadow_instance_id'] == identity
        assert restored['opened_at'] == now and restored['buy_price_usd'] == price
        assert restored['vec']['score_total'] == 73. and restored['vec']['nullable_input'] is None
        assert dt.datetime.fromisoformat(str(restored['vec']['timestamp']).replace('Z', '+00:00')) == vector['timestamp']
        assert vector['address'] in research._OPEN_SHADOWS
    ns['_tick_shadows'] = inspect
    await one_monitor_tick(ns, monkeypatch)
    events = [json.loads(line) for line in research.RESEARCH_EVENTS_PATH.read_text().splitlines()]
    assert len(events) == 1 and events[0]['decision_action'] == 'research_shadow_open'
    assert not list(store.DATA_DIR.glob('*.parquet'))


def state_fixture(root, monkeypatch, *, address='synthetic-owned-state'):
    from test_shadow_close_recovery import native_paths
    from runtime import shadow_state as states
    store, research = native_paths(root, monkeypatch)
    now = dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc)
    identity = uuid.uuid4().hex
    state = {'shadow_instance_id': identity, 'opened_at': now, 'buy_price_usd': 1.,
        'vec': {'address': address, 'timestamp': now, 'nullable_input': None},
        'entry_regime': 'pump_early', 'partial_taken': False,
        'remaining_fraction': 1., 'realized_pnl_ratio': 0., 'max_pnl_pct_seen': 0.}
    opening = {'id': f'shadow_state_{identity}_opening', 'observed_at': now,
        'payload': {'run_id': 'SYNTHETIC-ORIGINAL', 'shadow_instance_id': identity, 'opened_at': now},
        'shadow_kind': 'research'}
    envelope = states.checkpoint(root, address, state, context={'run_id': 'SYNTHETIC-ORIGINAL'}, opening=opening)
    state['_shadow_state'] = envelope
    return states, state, store, research, address


@pytest.mark.parametrize('failure', ['before_event', 'after_event', 'marker_ack'])
def test_original_open_event_retries_once_with_its_original_clock(tmp_path, monkeypatch, failure):
    states, state, store, research, address = state_fixture(tmp_path, monkeypatch)
    from features.decision_store import append_decision
    ledger = tmp_path/'metrics/decisions.jsonl'
    monkeypatch.setattr(research, 'append_decision', lambda row, **kwargs:
        append_decision(row, path=ledger, **kwargs))
    original = state['_shadow_state']
    with monkeypatch.context() as fault:
        def fail(*args, **kwargs): raise OSError('synthetic opening failure')
        if failure == 'before_event': fault.setattr(research, '_write_event', fail)
        elif failure == 'after_event':
            actual = research._write_event
            def event(*args, **kwargs):
                actual(*args, **kwargs)
                fail()
            fault.setattr(research, '_write_event', event)
        else:
            fault.setattr(states, 'write_json_atomic', fail)
        with pytest.raises(OSError): states.publish_pending(tmp_path, original, research=research)
    research._OPEN_SHADOWS.clear()
    restored = states.restore(tmp_path, research=research)['positions'][address]
    assert restored['shadow_instance_id'] == state['shadow_instance_id']
    assert restored['_shadow_state']['record']['published']['opening'] is True
    rows = [json.loads(line) for line in research.RESEARCH_EVENTS_PATH.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['ts_utc'] == state['opened_at'].isoformat()
    assert rows[0]['run_id'] == 'SYNTHETIC-ORIGINAL'
    assert len(ledger.read_text().splitlines()) == 1
    assert not list(store.DATA_DIR.glob('*.parquet'))


@pytest.mark.parametrize('field', ['timestamp', 'nullable_input', 'address'])
def test_checkpoint_rejects_changed_original_features_without_rewriting(tmp_path, monkeypatch, field):
    states, state, store, research, address = state_fixture(tmp_path, monkeypatch)
    path = states._paths(tmp_path, address)[1]
    before = path.read_bytes()
    state['vec'][field] = 'changed'
    with pytest.raises(ValueError, match='Original shadow entry changed'):
        states.checkpoint(tmp_path, address, state)
    assert path.read_bytes() == before


def test_stale_generation_cannot_replace_a_newer_peak(tmp_path, monkeypatch):
    states, state, store, research, address = state_fixture(tmp_path, monkeypatch)
    stale = deepcopy(state)
    state['max_pnl_pct_seen'] = 25000.
    state['_shadow_state'] = states.checkpoint(tmp_path, address, state)
    stale['max_pnl_pct_seen'] = 100.
    with pytest.raises(ValueError, match='Stale shadow state generation'):
        states.checkpoint(tmp_path, address, stale)
    assert states._read(states._paths(tmp_path, address)[1])['record']['state']['max_pnl_pct_seen'] == 25000.


def test_address_slot_cannot_be_stolen_by_another_instance(tmp_path, monkeypatch):
    states, state, store, research, address = state_fixture(tmp_path, monkeypatch)
    changed = deepcopy(state)
    changed.pop('_shadow_state')
    changed['shadow_instance_id'] = uuid.uuid4().hex
    with pytest.raises(ValueError, match='another instance'):
        states.checkpoint(tmp_path, address, changed)


@pytest.mark.parametrize('fault', ['checksum', 'path', 'oversize'])
def test_corrupt_active_state_is_retained_and_not_backfilled(tmp_path, monkeypatch, fault):
    states, state, store, research, address = state_fixture(tmp_path, monkeypatch)
    path = states._paths(tmp_path, address)[1]
    if fault == 'checksum':
        payload = json.loads(path.read_text())
        payload['record']['state']['max_pnl_pct_seen'] = 99999.
        path.write_text(json.dumps(payload))
    elif fault == 'path': path.rename(path.with_name('wrong.json')); path = path.with_name('wrong.json')
    else: path.write_text('x' * (states.MAX_BYTES + 4097))
    before = path.read_bytes()
    result = states.restore(tmp_path, research=research)
    assert not result['positions'] and len(result['failed']) == 1
    assert path.read_bytes() == before and not research._OPEN_SHADOWS
    assert not list(store.DATA_DIR.glob('*.parquet'))


def test_legacy_portfolio_without_original_state_is_not_reconstructed(tmp_path, monkeypatch):
    from test_shadow_close_recovery import native_paths
    from runtime.shadow_state import restore
    store, research = native_paths(tmp_path, monkeypatch)
    research.record_shadow_open('legacy', payload={'opened_at': '2026-10-01T00:00:00Z'}, shadow_kind='research')
    research._OPEN_SHADOWS.clear()
    assert restore(tmp_path, research=research) == {'positions': {}, 'failed': [], 'cursor': None}


def test_native_process_restart_restores_saved_state_without_opening_again(tmp_path, monkeypatch):
    states, state, store, research, address = state_fixture(tmp_path, monkeypatch)
    states.publish_pending(tmp_path, state['_shadow_state'], research=research)
    code = '''
import json, sys
from pathlib import Path
from analytics import research_runtime as research
from runtime.shadow_state import restore
root=Path(sys.argv[1])
research.METRICS_DIR=root/'metrics'
research.RESEARCH_EVENTS_PATH=root/'metrics/events.jsonl'
research.RESEARCH_PORTFOLIO_PATH=root/'portfolio.json'
research.append_decision=lambda *args, **kwargs: None
assert not research._OPEN_SHADOWS
result=restore(root, research=research)
assert not result['failed']
state=next(iter(result['positions'].values()))
print(json.dumps({'id':state['shadow_instance_id'], 'opened_at':state['opened_at'].isoformat(),
 'vector':state['vec'], 'context':research._OPEN_SHADOWS[sys.argv[2]]}))
'''
    child = subprocess.run([sys.executable, '-c', code, str(tmp_path), address],
        text=True, capture_output=True, timeout=30, check=True)
    result = json.loads(child.stdout.strip().splitlines()[-1])
    assert result['id'] == state['shadow_instance_id'] and result['opened_at'] == state['opened_at'].isoformat()
    assert result['vector']['nullable_input'] is None
    assert result['context']['run_id'] == 'SYNTHETIC-ORIGINAL'
    assert len(research.RESEARCH_EVENTS_PATH.read_text().splitlines()) == 1


@pytest.mark.asyncio
async def test_retirement_ack_failure_restarts_same_close_without_refetch_or_context_leak(tmp_path, monkeypatch):
    from runtime import shadow_state as states
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    await ns['_open_shadow']('synthetic-retirement', {'address': 'synthetic-retirement', 'timestamp': now},
        price_hint=1., force=True, regime='pump_early')
    ns['exit_policy'].should_exit = lambda *args, **kwargs: 'TIMEOUT'
    with monkeypatch.context() as fault:
        fault.setattr(states, 'retire', Mock(side_effect=OSError('synthetic lost retire ack')))
        await ns['_tick_shadows']()
    original_close = deepcopy(ns['_shadow_positions']['synthetic-retirement']['close_observation'])
    assert len(list((tmp_path/'data/research/shadow_close_outbox/complete').glob('*.json'))) == 1
    ns['_shadow_positions'].clear()
    research._OPEN_SHADOWS.clear()
    ns['price_service'].get_price.reset_mock()
    await one_monitor_tick(ns, monkeypatch)
    assert not ns['_shadow_positions'] and not research._OPEN_SHADOWS
    ns['price_service'].get_price.assert_not_awaited()
    import pandas as pd
    frame = pd.read_parquet(next(store.DATA_DIR.glob('*.parquet')))
    assert len(frame) == 1 and frame.outcome_observation_id.iloc[0] == original_close['id']
    rows = [json.loads(line) for line in research.RESEARCH_EVENTS_PATH.read_text().splitlines()]
    assert len(rows) == 2 and sum(row['event_type'] == 'candidate_outcome' for row in rows) == 1


@pytest.mark.asyncio
async def test_partial_write_failure_keeps_original_pending_event_for_retry(tmp_path, monkeypatch):
    from runtime import shadow_state as states
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    await ns['_open_shadow']('synthetic-partial-write', {'address': 'synthetic-partial-write', 'timestamp': now},
        price_hint=1., force=True, regime='pump_early')
    ns['exit_policy'].should_take_partial = lambda subject, pnl: not subject.partial_taken
    ns['exit_policy'].partial_fraction = lambda subject: .5
    actual = states.write_json_atomic
    def fail_partial(path, value):
        if value['record'].get('partial') is not None: raise OSError('synthetic partial admission failure')
        return actual(path, value)
    with monkeypatch.context() as fault:
        fault.setattr(states, 'write_json_atomic', fail_partial)
        await ns['_tick_shadows']()
    original = deepcopy(ns['_shadow_positions']['synthetic-partial-write']['_shadow_pending_partial'])
    ns['utc_now'] = lambda: now + dt.timedelta(minutes=5)
    await ns['_tick_shadows']()
    saved = ns['_shadow_positions']['synthetic-partial-write']['_shadow_state']['record']
    assert saved['partial']['observed_at'] == original['observed_at'].isoformat()
    assert saved['state']['remaining_fraction'] == .5 and saved['state']['realized_pnl_ratio'] == .5
    events = [json.loads(line) for line in research.RESEARCH_EVENTS_PATH.read_text().splitlines()]
    assert sum(row['event_type'] == 'candidate_partial' for row in events) == 1


def test_original_series_proof_attrs_and_nulls_survive_state_storage(tmp_path, monkeypatch):
    import pandas as pd
    from features.auxiliary_semantics import PROOF_COLUMN
    from features.token_clock_semantics import PROOF_COLUMN as CLOCK
    from features.valuation_semantics import PROOF_COLUMN as VALUATION
    from test_shadow_close_recovery import native_paths
    from runtime import shadow_state as states
    store, research = native_paths(tmp_path, monkeypatch)
    clock = dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc)
    vector = pd.Series({'address': 'synthetic-attrs', 'timestamp': clock, 'nullable_input': pd.NA})
    vector.attrs = {name: {'marker': name, 'synthetic_not_authenticated': True} for name in (PROOF_COLUMN, CLOCK, VALUATION)}
    state = {'shadow_instance_id': uuid.uuid4().hex, 'opened_at': clock, 'buy_price_usd': None, 'vec': vector}
    saved = states.checkpoint(tmp_path, 'synthetic-attrs', state)
    restored = states.restore(tmp_path, research=research)['positions']['synthetic-attrs']
    assert restored['vec']['nullable_input'] is None
    assert all(restored['vec'][name] == vector.attrs[name] for name in vector.attrs)
    assert restored['_shadow_state'] == saved and not list(store.DATA_DIR.glob('*.parquet'))


@pytest.mark.parametrize('budget', [True, 0, 257, None])
def test_invalid_recovery_budget_cannot_trigger_any_io(tmp_path, budget):
    from runtime.shadow_state import restore
    with pytest.raises(ValueError): restore(tmp_path, max_items=budget)
    assert not list(tmp_path.iterdir())


def test_bounded_restore_cursor_visits_other_states_after_corrupt_head(tmp_path, monkeypatch):
    states, first, store, research, a = state_fixture(tmp_path, monkeypatch, address='synthetic-first')
    _, second, _, _, b = state_fixture(tmp_path, monkeypatch, address='synthetic-second')
    files = sorted(states._paths(tmp_path, a)[1].parent.glob('*.json'))
    broken = files[0]
    broken.write_text('{}')
    result = states.restore(tmp_path, max_items=1, research=research)
    assert result['failed'] and not result['positions']
    next_result = states.restore(tmp_path, max_items=1, after=result['cursor'], research=research)
    assert len(next_result['positions']) == 1 and not next_result['failed'] and broken.exists()


def test_shadow_ledger_conflict_is_rejected_without_an_extra_row(tmp_path):
    from features.decision_store import append_decision
    key = 'shadow_state_' + uuid.uuid4().hex + '_opening'
    row = {'address': 'synthetic-ledger', 'timestamp': '2026-10-14T00:00:00Z',
        'action': 'shadow', 'shadow_state_observation_id': key}
    path = tmp_path/'ledger.jsonl'
    append_decision(row, path=path, idempotency_key=key)
    append_decision(row, path=path, idempotency_key=key)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='Conflicting'):
        append_decision({**row, 'reason': 'different'}, path=path, idempotency_key=key)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_owned_open_io_cancellation_drains_and_restores_original_state(tmp_path, monkeypatch):
    from runtime import shadow_state as states
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    actual = states.checkpoint
    def delayed(*args, **kwargs):
        result = actual(*args, **kwargs)
        started.set()
        assert release.wait(10)
        return result
    monkeypatch.setattr(states, 'checkpoint', delayed)
    task = asyncio.create_task(ns['_open_shadow']('synthetic-cancel-open',
        {'address': 'synthetic-cancel-open', 'timestamp': now}, price_hint=1., force=True))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert not ns['_shadow_positions']
    recovered = states.restore(tmp_path, research=research)
    assert list(recovered['positions']) == ['synthetic-cancel-open'] and not recovered['failed']


@pytest.mark.parametrize('result', [False, None, 1])
def test_unconfirmed_open_effect_does_not_acknowledge_saved_state(tmp_path, monkeypatch, result):
    states, state, store, research, address = state_fixture(tmp_path, monkeypatch)
    fake = SimpleNamespace(record_shadow_open=Mock(return_value=result), restore_shadow_context=Mock())
    with pytest.raises(OSError, match='unconfirmed'):
        states.publish_pending(tmp_path, state['_shadow_state'], research=fake)
    assert states._read(states._paths(tmp_path, address)[1])['record']['published']['opening'] is False
    fake.restore_shadow_context.assert_not_called()


def test_original_frozen_close_cannot_be_replaced_by_a_later_observation(tmp_path, monkeypatch):
    from runtime.shadow_closes import freeze_observation
    states, state, store, research, address = state_fixture(tmp_path, monkeypatch)
    state['close_observation'] = freeze_observation(address, state['vec'], closed_at=state['opened_at'],
        close={'regime': 'pump_early', 'pnl_pct': 50., 'label': 1, 'exit_reason': 'SYNTHETIC',
            'close_price_usd': 1.5, 'shadow_kind': 'research', 'extra': {}},
        context={}, instance_id=state['shadow_instance_id'])
    state['_shadow_state'] = states.checkpoint(tmp_path, address, state)
    original = states._paths(tmp_path, address)[1].read_bytes()
    state['close_observation']['close']['pnl_pct'] = 99999.
    with pytest.raises(ValueError, match='Original shadow close changed'):
        states.checkpoint(tmp_path, address, state)
    assert states._paths(tmp_path, address)[1].read_bytes() == original


@pytest.mark.parametrize('value', ['oversize', 'unsupported'])
def test_invalid_initial_snapshot_cannot_create_state_files(tmp_path, value):
    from runtime import shadow_state as states
    state = {'shadow_instance_id': uuid.uuid4().hex, 'opened_at': dt.datetime.now(dt.timezone.utc),
        'vec': {'address': 'synthetic-invalid', 'value': 'x'*(states.MAX_BYTES+1) if value == 'oversize' else object()}}
    with pytest.raises((ValueError, TypeError)): states.checkpoint(tmp_path, 'synthetic-invalid', state)
    assert not list(tmp_path.iterdir())


def test_cold_state_import_has_no_disk_worker_provider_or_child_process_effect(tmp_path):
    root = Path.cwd()
    code = '''
import json, socket, subprocess, sys, threading
from pathlib import Path
sys.path.insert(0, sys.argv[1])
def denied(*args, **kwargs): raise AssertionError('Import attempted an external effect')
socket.socket=denied
subprocess.Popen=denied
before=len(threading.enumerate())
import runtime.shadow_state
assert len(threading.enumerate()) == before
assert not list(Path.cwd().iterdir())
print(json.dumps({'disk_entries':0, 'new_threads':0}))
'''
    child = subprocess.run([sys.executable, '-c', code, str(root)], cwd=tmp_path,
        text=True, capture_output=True, timeout=30, check=True)
    assert json.loads(child.stdout.strip()) == {'disk_entries': 0, 'new_threads': 0}


@pytest.mark.asyncio
async def test_open_original_vector_is_detached_before_slow_price_lookup(tmp_path, monkeypatch):
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args, **kwargs):
        started.set()
        await release.wait()
        return 1.
    ns['price_service'].get_price_usd = AsyncMock(side_effect=delayed)
    vector = {'address': 'synthetic-slow-open', 'timestamp': now, 'score_total': 73.}
    task = asyncio.create_task(ns['_open_shadow'](vector['address'], vector, force=True))
    await started.wait()
    vector['score_total'] = -999.
    release.set()
    await task
    assert ns['_shadow_positions'][vector['address']]['vec']['score_total'] == 73.


@pytest.mark.asyncio
async def test_admitted_large_vector_can_freeze_and_finish_its_native_close(tmp_path, monkeypatch):
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    vector = {'address': 'synthetic-large-state', 'timestamp': now, 'synthetic_payload': 'x'*600000}
    await ns['_open_shadow'](vector['address'], vector, price_hint=1., force=True)
    assert vector['address'] in ns['_shadow_positions']
    ns['exit_policy'].should_exit = lambda *args, **kwargs: 'SYNTHETIC_CLOSE'
    await ns['_tick_shadows']()
    assert not ns['_shadow_positions']
    assert len(list(store.DATA_DIR.glob('*.parquet'))) == 1


@pytest.mark.asyncio
async def test_native_monitor_recovers_original_partial_prefix_after_restart(tmp_path, monkeypatch):
    ns, store, research, now = native_namespace(tmp_path, monkeypatch)
    await ns['_open_shadow']('synthetic-partial', {'address': 'synthetic-partial', 'timestamp': now},
        price_hint=1., force=True, regime='pump_early')
    ns['exit_policy'].should_take_partial = lambda subject, pnl: not subject.partial_taken
    ns['exit_policy'].partial_fraction = lambda subject: .5
    await ns['_tick_shadows']()
    original = ns['_shadow_positions']['synthetic-partial']
    assert original['remaining_fraction'] == .5 and original['realized_pnl_ratio'] == .5
    ns['_shadow_positions'].clear()
    research._OPEN_SHADOWS.clear()
    async def inspect():
        assert 'synthetic-partial' in ns['_shadow_positions'], 'Original partial prefix was lost across restart'
        restored = ns['_shadow_positions']['synthetic-partial']
        assert restored['partial_taken'] is True
        assert restored['remaining_fraction'] == .5 and restored['realized_pnl_ratio'] == .5
        assert restored['max_pnl_pct_seen'] == 100.
    ns['_tick_shadows'] = inspect
    await one_monitor_tick(ns, monkeypatch)
    events = [json.loads(line) for line in research.RESEARCH_EVENTS_PATH.read_text().splitlines()]
    assert len(events) == 2 and sum(row['event_type'] == 'candidate_partial' for row in events) == 1
    assert not list(store.DATA_DIR.glob('*.parquet'))
