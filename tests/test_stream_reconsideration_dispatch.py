"""Native queue -> native guard -> native entry prefix, no providers or orders."""
from __future__ import annotations

import ast
import asyncio
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import runtime.hot_queue as queue_module
from runtime.hot_queue import HotQueue
from runtime.loop_scheduler import evaluate_hot_queue
from test_entry_observation import MINT, observed, preparation_namespace


def function(name):
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'run_bot.py').read_text(encoding='utf-8'))
    return next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)


@pytest.fixture
def native_dispatch(monkeypatch):
    import research_loop.entry_gate_policy as selected
    import research_loop.entry_gate_forward as capture
    import runtime.paper_entry_policy as policy

    # No policy/model/source activation is being tested or granted here. Keep
    # the unchanged outer managers inert and execute the real native dispatch
    # plus entry prefix through fresh-observation freezing, never a buy body.
    monkeypatch.setattr(selected, 'selected_scope', lambda *a, **k: nullcontext())
    monkeypatch.setattr(capture, 'capture_scope', lambda *a, **k: nullcontext())
    monkeypatch.setattr(policy, 'snapshot', lambda: None)
    now = [1_792_000_000.0]
    monkeypatch.setattr(queue_module, 'CFG', SimpleNamespace(HOT_QUEUE_RECHECK_INTERVAL_S=15,
        HOT_QUEUE_DYNAMIC_BATCH_ENABLED=False, HOT_QUEUE_LOW_PRIORITY_MAX_AGE_MIN=20,
        HOT_QUEUE_HIGH_PRIORITY_MAX_AGE_MIN=20, HOT_QUEUE_HIGH_PRIORITY_MIN_SCORE=75))
    monkeypatch.setattr(queue_module, 'runner_priority_generation', lambda: 'checked-family-1')
    monkeypatch.setattr(queue_module, 'learned_runner_priorities', lambda rows, **k: [{'bonus': 0.} for _ in rows])
    monkeypatch.setattr(queue_module, 'candidate_priority_score', lambda token, **k: 80.)
    queue = HotQueue(max_size=4)
    monkeypatch.setattr(queue, '_now', lambda: now[0])
    namespace, waits, fresh = preparation_namespace(observed())
    errors, events = [], []
    namespace.update(GLOBAL_HOT_QUEUE=queue, PROJECT_ROOT=Path('unused-isolated-context'),
        _BUY_RECOVERY=SimpleNamespace(scope=lambda: nullcontext()),
        _stream_candidate_cooldown_until={},
        time=SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0]),
        EVALUATE_TOKEN_TIMEOUT_S=0, asyncio=asyncio,
        _note_runtime_error=lambda *a: errors.append(a),
        record_runtime_event=lambda *a, **k: events.append((a,k)),
        log=SimpleNamespace(error=lambda *a: None, debug=lambda *a: None))
    namespace['_evaluate_and_buy'] = namespace['prepare']
    for name in ('_prune_expiring', '_stream_candidate_is_cooled', '_evaluate_and_buy_guarded'):
        exec(compile(ast.Module(body=[function(name)], type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))

    async def dispatch(token, *, source='hot_queue'):
        await namespace['_evaluate_and_buy_guarded'](token, session, source=source)

    def candidate(**extra):
        return dict(address=MINT, discovered_via='pumpfun', age_minutes=1,
            price_pct_5m=10., txns_last_5m=10, price_usd=2., **extra)

    return SimpleNamespace(queue=queue, now=now, namespace=namespace, fresh=fresh,
        dispatch=dispatch, candidate=candidate, errors=errors, waits=waits, events=events)


@pytest.mark.asyncio
async def test_native_changed_queue_signal_reaches_fresh_collection_inside_original_blackout(native_dispatch):
    n = native_dispatch
    initial = n.candidate()
    assert n.queue.add(initial) and n.queue.pop_batch(1)
    n.namespace['_stream_candidate_cooldown_until'][MINT] = n.now[0] + 900
    n.now[0] += 16
    improved = {**initial, 'price_pct_5m': 200., 'txns_last_5m': 200}
    assert n.queue.add(improved), 'Existing native queue already admits this changed signal'
    assert await evaluate_hot_queue(n.queue, n.dispatch, max_items=1, budget_s=1) == 1
    assert n.errors == []
    assert n.fresh.await_count == 1, 'Native entry must not silently discard a checked changed dispatch'
    assert n.namespace['_stream_candidate_is_cooled'](MINT), 'Do not erase the original cooldown for other callers'


@pytest.mark.asyncio
async def test_source_label_or_injected_payload_does_not_bypass_original_cooldown(native_dispatch):
    n = native_dispatch
    n.namespace['_stream_candidate_cooldown_until'][MINT] = n.now[0] + 900
    forged = {**n.candidate(), '_hot_queue_reconsideration': True, 'stream_reconsideration': True}
    await n.dispatch(forged)
    assert n.errors == [] and n.fresh.await_count == 0


@pytest.mark.asyncio
async def test_non_hot_source_cannot_consume_a_real_queue_dispatch(native_dispatch):
    n = native_dispatch
    assert n.queue.add(n.candidate()) and n.queue.pop_batch(1)
    n.namespace['_stream_candidate_cooldown_until'][MINT] = n.now[0] + 900
    n.now[0] += 16
    assert n.queue.add({**n.candidate(), 'price_pct_5m': 200})
    row = n.queue.pop_batch(1)[0]
    await n.dispatch(row, source='queue')
    assert n.errors == [] and n.fresh.await_count == 0


@pytest.mark.asyncio
async def test_queue_reconsideration_is_bound_to_the_actual_popped_object(native_dispatch):
    n = native_dispatch
    assert n.queue.add(n.candidate()) and n.queue.pop_batch(1)
    n.namespace['_stream_candidate_cooldown_until'][MINT] = n.now[0] + 900
    n.now[0] += 16
    assert n.queue.add({**n.candidate(), 'price_pct_5m': 200})
    row = n.queue.pop_batch(1)[0]
    await n.dispatch(deepcopy(row))
    assert n.errors == [] and n.fresh.await_count == 0
    await n.dispatch(row)
    assert n.errors == [] and n.fresh.await_count == 1


@pytest.mark.asyncio
async def test_one_real_queue_dispatch_cannot_be_replayed(native_dispatch):
    n = native_dispatch
    assert n.queue.add(n.candidate()) and n.queue.pop_batch(1)
    n.namespace['_stream_candidate_cooldown_until'][MINT] = n.now[0] + 900
    n.now[0] += 16
    assert n.queue.add({**n.candidate(), 'price_pct_5m': 200})
    row = n.queue.pop_batch(1)[0]
    await n.dispatch(row)
    assert n.errors == [] and n.fresh.await_count == 1
    await n.dispatch(row)
    assert n.errors == [] and n.fresh.await_count == 1


def changed_dispatch(n):
    assert n.queue.add(n.candidate()) and n.queue.pop_batch(1)
    n.namespace['_stream_candidate_cooldown_until'][MINT] = n.now[0] + 900
    n.now[0] += 16
    assert n.queue.add({**n.candidate(), 'price_pct_5m': 200})
    return n.queue.pop_batch(1)[0]


@pytest.mark.asyncio
async def test_first_queue_dispatch_cannot_override_an_existing_blackout(native_dispatch):
    n = native_dispatch
    n.namespace['_stream_candidate_cooldown_until'][MINT] = n.now[0] + 900
    assert n.queue.add(n.candidate())
    await n.dispatch(n.queue.pop_batch(1)[0])
    assert n.errors == [] and n.fresh.await_count == 0


@pytest.mark.asyncio
async def test_unchanged_dispatch_after_dedup_ttl_is_not_changed_signal_permission(native_dispatch):
    n = native_dispatch
    n.queue.dedup_ttl_s = 20
    assert n.queue.add(n.candidate()) and n.queue.pop_batch(1)
    n.namespace['_stream_candidate_cooldown_until'][MINT] = n.now[0] + 900
    n.now[0] += 21
    assert n.queue.add(n.candidate())
    await n.dispatch(n.queue.pop_batch(1)[0])
    assert n.errors == [] and n.fresh.await_count == 0


@pytest.mark.parametrize('change', ['price_pct_5m', 'age_minutes', 'source', '_hot_queue_enqueued_at', '_hot_queue_priority_generation', 'learned_runner_priority'])
def test_post_pop_mutation_invalidates_original_dispatch_once(native_dispatch, change):
    n = native_dispatch
    row = changed_dispatch(n)
    original = deepcopy(row.get(change))
    row[change] = 'changed-after-pop'
    assert n.queue.consume_reconsideration(row) is False
    row[change] = original
    assert n.queue.consume_reconsideration(row) is False


@pytest.mark.parametrize('elapsed', [-1., 16., float('nan'), float('inf')])
def test_old_or_invalid_dispatch_clock_cannot_override_blackout(native_dispatch, elapsed):
    n = native_dispatch
    row = changed_dispatch(n)
    n.now[0] += elapsed
    assert n.queue.consume_reconsideration(row) is False
    assert n.queue.consume_reconsideration(row) is False


@pytest.mark.parametrize('fraction', [0.0000002, 0.0000007, 0.0000017])
def test_dispatch_retains_original_numeric_clock_without_utc_rounding(native_dispatch, fraction):
    n = native_dispatch
    n.now[0] += fraction
    row = changed_dispatch(n)
    assert n.queue.consume_reconsideration(row) is True


def test_new_update_supersedes_an_old_unconsumed_dispatch(native_dispatch):
    n = native_dispatch
    row = changed_dispatch(n)
    assert not n.queue.add(n.candidate())  # Too soon to reevaluate, but a new input is now known.
    assert n.queue.consume_reconsideration(row) is False


def test_dispatch_registry_remains_bounded_without_consumers(native_dispatch):
    n = native_dispatch
    for index in range(20):
        assert n.queue.add({**n.candidate(), 'address': f'candidate-{index}'})
        assert n.queue.pop_batch(1)
    n.now[0] += 16
    dispatched = []
    for index in range(20):
        assert n.queue.add({**n.candidate(), 'address': f'candidate-{index}', 'price_pct_5m': 200})
        dispatched.extend(n.queue.pop_batch(1))
    assert len(n.queue._reconsiderations) == n.queue.max_size == 4
    assert n.queue.consume_reconsideration(dispatched[0]) is False
    assert n.queue.consume_reconsideration(dispatched[-1]) is True


def test_recheck_interval_is_not_bypassed_by_changed_payload(native_dispatch):
    n = native_dispatch
    assert n.queue.add(n.candidate()) and n.queue.pop_batch(1)
    n.now[0] += 1
    assert not n.queue.add({**n.candidate(), 'price_pct_5m': 200})
    assert n.queue.pop_batch(1) == []


@pytest.mark.asyncio
async def test_changed_dispatch_still_waits_when_fresh_provider_snapshot_is_absent(native_dispatch):
    n = native_dispatch
    row = changed_dispatch(n)
    n.fresh.return_value = None
    await n.dispatch(row)
    assert n.errors == [] and n.fresh.await_count == 1
    assert n.waits == [{'reason': 'snapshot_unavailable', 'stage': 'entry_snapshot'}]


@pytest.mark.asyncio
async def test_changed_dispatch_still_waits_for_verified_original_age(native_dispatch):
    n = native_dispatch
    row = changed_dispatch(n)
    n.namespace['_candidate_age_minutes'] = lambda *a: None
    await n.dispatch(row)
    assert n.errors == [] and n.fresh.await_count == 1
    assert n.waits == [{'reason': 'missing_age', 'stage': 'entry_snapshot'}]
    await n.dispatch(row)
    assert n.errors == [] and n.fresh.await_count == 1


@pytest.mark.asyncio
async def test_shadow_followup_source_cannot_claim_owned_changed_dispatch(native_dispatch):
    n = native_dispatch
    n.namespace['_stream_candidate_cooldown_until'][MINT] = n.now[0] + 900
    token = {**n.candidate(), 'discovered_via':'shadow_followup', 'stream_reconsideration':True}
    await n.dispatch(token)
    assert n.errors == [] and n.fresh.await_count == 0


@pytest.mark.asyncio
async def test_owned_changed_shadow_followup_collects_fresh_inputs_without_erasing_blackout(native_dispatch):
    n = native_dispatch
    initial = {**n.candidate(), 'discovered_via':'shadow_followup'}
    assert n.queue.add(initial) and n.queue.pop_batch(1)
    n.namespace['_stream_candidate_cooldown_until'][MINT] = n.now[0] + 900
    n.now[0] += 16
    assert n.queue.add({**initial, 'price_pct_5m':300.})
    await n.dispatch(n.queue.pop_batch(1)[0])
    assert n.errors == [] and n.fresh.await_count == 1
    assert n.namespace['_stream_candidate_is_cooled'](MINT)
