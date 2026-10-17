"""Local scanner admission and actual native queue integration; no providers."""
from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import runpy

import pytest

from runtime.price_attempt_admission import PriceAttemptAdmission, retry_delay_seconds


def controlled(**kwargs):
    now = [1000.]
    return PriceAttemptAdmission(clock=lambda: now[0], **kwargs), now


def test_first_attempt_at_zero_has_no_invented_previous_attempt():
    admission = PriceAttemptAdmission(clock=lambda: 0.)
    assert admission.admit('A').allowed


def test_combined_wait_uses_the_later_constraint_without_reserving_again():
    admission, now = controlled(quota=1)
    assert admission.admit('A').allowed
    first = admission.admit('A')
    assert not first.allowed and first.reason == 'token_and_window_wait' and first.retry_after_s == 25
    assert admission.admit('B').reason == 'window_capacity'
    now[0] += 10
    again = admission.admit('A')
    assert again.reason == 'token_cooldown' and again.retry_after_s == 15
    assert admission.admit('B').allowed
    now[0] += 15
    assert admission.admit('A').allowed


@pytest.mark.parametrize('quota', [1, 4, 64, 65, 100, 4096])
def test_entire_configured_window_is_enforced_without_deque_truncation(quota):
    admission, now = controlled(quota=quota, capacity=8192)
    assert sum(admission.admit(f'mint-{i}').allowed for i in range(quota + 7)) == quota
    assert admission.snapshot()['attempts_in_window'] == quota
    now[0] += 10
    assert admission.admit('new').allowed


def test_denied_key_never_spends_or_extends_an_admission():
    admission, now = controlled()
    assert admission.admit('A').allowed
    for second in (1, 5, 10, 20, 24):
        now[0] = 1000 + second
        denied = admission.admit('A')
        assert denied.retry_after_s == 25 - second
    now[0] = 1025
    assert admission.admit('A').allowed


def test_old_per_token_state_is_swept_instead_of_growing_with_the_run():
    admission, now = controlled()
    for i in range(10000):
        assert admission.admit(f'mint-{i}').allowed
        now[0] += 30
    state = admission.snapshot()
    assert state['cooling_tokens'] == state['attempts_in_window'] == 0


def test_capacity_cannot_evict_an_unexpired_cooldown_or_punish_an_existing_key():
    admission, now = controlled(quota=10, capacity=2)
    assert admission.admit('A').allowed
    now[0] += 2
    assert admission.admit('B').allowed
    blocked = admission.admit('C')
    assert blocked.reason == 'state_capacity' and blocked.retry_after_s == 23
    assert admission.admit('A').reason == 'token_cooldown'
    assert admission.snapshot()['cooling_tokens'] == 2
    now[0] += 23
    assert admission.admit('C').allowed
    assert admission.admit('B').reason == 'token_cooldown'


@pytest.mark.parametrize('bad', [None, True, float('nan'), float('inf'), -1., '1001'])
def test_bad_or_backwards_clocks_deny_without_refreshing_state(bad):
    admission, now = controlled()
    assert admission.admit('A').allowed
    now[0] = bad
    assert admission.admit('new').reason == 'clock_unavailable'
    now[0] = 1005
    assert admission.admit('A').retry_after_s == 20
    assert admission.admit('new').allowed


def test_backwards_positive_clock_and_clock_exception_preserve_original_expiry():
    admission, now = controlled()
    assert admission.admit('A').allowed
    now[0] = 999
    assert admission.admit('B').reason == 'clock_unavailable'
    def broken(): raise RuntimeError('clock failed')
    admission.clock = broken
    assert admission.admit('B').reason == 'clock_unavailable'
    admission.clock = lambda: 1025.
    assert admission.admit('A').allowed


def test_unrepresentable_clock_arithmetic_fails_closed():
    admission = PriceAttemptAdmission(clock=lambda: 1e308)
    assert admission.admit('A').reason == 'clock_unavailable'


@pytest.mark.parametrize('bad', ['', 'bad', True, None, float('nan'), float('inf')])
def test_invalid_optional_configuration_has_native_safe_defaults(bad):
    admission = PriceAttemptAdmission.from_env({'PUMPFUN_PRICE_QUOTA': bad,
        'PUMPFUN_PRICE_QUOTA_WINDOW': bad, 'PUMPFUN_PRICE_ATTEMPT_COOLDOWN': bad}, clock=lambda: 0.)
    state = admission.snapshot()
    assert state['quota'] == 4 and state['window_s'] == 10 and state['cooldown_s'] == 25
    assert admission.admit('A').allowed


def test_fractional_seconds_are_valid_but_fractional_attempt_count_is_not():
    admission = PriceAttemptAdmission.from_env({'PUMPFUN_PRICE_QUOTA': '1.5',
        'PUMPFUN_PRICE_QUOTA_WINDOW': '1.5', 'PUMPFUN_PRICE_ATTEMPT_COOLDOWN': '2.5'}, clock=lambda: 0.)
    state = admission.snapshot()
    assert state['quota'] == 4 and state['window_s'] == 1.5 and state['cooldown_s'] == 2.5
    assert admission.admit('A').allowed
    assert admission.admit('A').retry_after_s == 2.5


@pytest.mark.parametrize('quota', [0, '0', -1, '-5'])
def test_explicit_nonpositive_quota_remains_disabled(quota):
    admission, _ = controlled(quota=quota)
    assert admission.admit('A').reason == 'disabled'
    assert admission.snapshot()['attempts_in_window'] == 0


def test_explicit_zero_cooldown_preserves_the_global_window():
    admission, _ = controlled(quota=4, cooldown_s=0)
    assert sum(admission.admit('A').allowed for _ in range(10)) == 4
    assert admission.snapshot()['cooling_tokens'] == 0


def test_concurrent_callers_share_exactly_one_process_window():
    admission, _ = controlled(quota=4)
    with ThreadPoolExecutor(max_workers=12) as executor:
        values = list(executor.map(admission.admit, [f'mint-{i}' for i in range(100)]))
    assert sum(v.allowed for v in values) == 4


@pytest.mark.parametrize('identity', [None, '', ' ', 'a b', '\tA', 'a' * 129, 5, True])
def test_invalid_identity_spends_no_budget(identity):
    admission, _ = controlled()
    assert admission.admit(identity).reason == 'invalid_identity'
    assert admission.snapshot()['attempts_in_window'] == 0


def test_decisions_and_snapshots_do_not_expose_or_mutate_keys():
    admission, _ = controlled()
    decision = admission.admit('private-candidate-identity')
    with pytest.raises(FrozenInstanceError):
        decision.allowed = False
    snapshot = admission.snapshot()
    assert 'private-candidate-identity' not in str(snapshot)
    snapshot['counts'].clear()
    assert admission.snapshot()['counts'] == {'admitted': 1}


@pytest.mark.parametrize('wait,delay', [(None, 5), (float('nan'), 5), (float('inf'), 5),
    (False, 5), ('bad', 5), (-1, 5), (0, 5), (.01, 1), (5, 5), (24.2, 25), (25, 25), (1e308, 3600)])
def test_queue_wakeup_is_bounded_and_never_rounds_a_normal_wait_down(wait, delay):
    assert retry_delay_seconds(wait) == delay


def native_defer(namespace):
    tree = ast.parse(Path('run_bot.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_defer_entry_observation')
    namespace['retry_delay_seconds'] = retry_delay_seconds
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'run_bot.py', 'exec'), namespace)
    return namespace['_defer_entry_observation']


@pytest.mark.parametrize('pending_delay,scheduled_delay', [(24.2, 25), (55, 55)])
def test_actual_native_wait_preserves_original_queue_age_retry_budget_and_input(monkeypatch, pending_delay, scheduled_delay):
    from utils import lista_pares as queue
    original_watch, original_processed = queue._pair_watch, queue._processed
    monkeypatch.setattr(queue, '_pair_watch', {})
    monkeypatch.setattr(queue, '_processed', set())
    monkeypatch.setattr(queue, 'log_queue_add', lambda *a, **k: None)
    monkeypatch.setattr(queue, 'log_queue_requeue', lambda *a, **k: None)
    monkeypatch.setattr(queue, 'log_queue_drop', lambda *a, **k: None)
    now = [1000.]
    monkeypatch.setattr(queue.time, 'time', lambda: now[0])
    assert queue.agregar_si_nuevo('A', retries=3)
    now[0] += 10
    token = {'address': 'A', 'mint_created_at': 500., 'creation_reference': {'original': True}}
    before = deepcopy(token)
    decisions = []
    pending = {'A': {'stale': True}}
    ns = {'_pending_ai_vectors': pending, '_research_decision': lambda *a, **k: decisions.append(k),
          '_ensure_requeue_with_stats': lambda addr, **k: queue.requeue(addr, reason=k['reason'], backoff=k['backoff'])}
    native_defer(ns)(token, reason='local_pumpfun_wait:token_cooldown', stage='entry_snapshot', retry_after_s=pending_delay)
    assert queue.meta('A')['first_seen'] == 1000
    assert queue.meta('A')['next_try'] == 1010 + scheduled_delay and queue.meta('A')['retries'] == 3
    assert decisions[0]['action'] == 'wait' and decisions[0]['dedup_ttl_s'] == scheduled_delay
    assert token == before and not pending
    assert queue._pair_watch is not original_watch and queue._processed is not original_processed


@pytest.mark.asyncio
@pytest.mark.parametrize('cooldown', [25, 60])
async def test_actual_native_entry_prefix_does_not_call_birth_or_market_until_local_wait_expires(cooldown):
    helpers = runpy.run_path('tests/test_entry_observation.py')
    ns, waits, market = helpers['preparation_namespace'](helpers['observed']())
    admission, now = controlled(quota=1, cooldown_s=cooldown)
    ns['_PF_PRICE_ADMISSION'] = admission
    token = {'address': helpers['MINT'], 'discovered_via': 'pumpfun'}
    before = deepcopy(token)
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    await ns['prepare'](token, session)
    assert market.await_count == ns['enrich_token_birth'].await_count == 1
    now[0] += 5
    assert await ns['prepare'](token, session) is None
    assert market.await_count == ns['enrich_token_birth'].await_count == 1
    assert waits[-1] == {'reason': 'local_pumpfun_wait:token_and_window_wait', 'stage': 'entry_snapshot', 'retry_after_s': cooldown - 5.}
    now[0] += cooldown - 5
    await ns['prepare'](token, session)
    assert market.await_count == ns['enrich_token_birth'].await_count == 2
    assert token == before


def test_native_runtime_snapshot_uses_the_actual_admission_owner():
    tree = ast.parse(Path('run_bot.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_build_runtime_state_snapshot')
    payload = next(n.value for n in ast.walk(node) if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == 'stats_payload' for t in n.targets))
    expression = payload.values[next(i for i, key in enumerate(payload.keys)
        if isinstance(key, ast.Constant) and key.value == 'local_price_attempt_admission')]
    admission, _ = controlled()
    admission.admit('A')
    actual = eval(compile(ast.fix_missing_locations(ast.Expression(expression)), 'native-stats', 'eval'), {'_PF_PRICE_ADMISSION': admission})
    assert actual['counts'] == {'admitted': 1} and actual['quota'] == 4


@pytest.mark.asyncio
async def test_native_shadow_followup_shares_original_pump_price_admission_and_waits_without_io():
    helpers = runpy.run_path('tests/test_entry_observation.py')
    ns, waits, market = helpers['preparation_namespace'](helpers['observed']())
    admission, now = controlled(quota=1, cooldown_s=25)
    ns['_PF_PRICE_ADMISSION'] = admission
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    token = {'address': helpers['MINT'], 'discovered_via':'pumpfun'}
    await ns['prepare'](token, session)
    now[0] += 5
    await ns['prepare']({**token, 'discovered_via':'shadow_followup'}, session)
    assert market.await_count == ns['enrich_token_birth'].await_count == 1
    assert waits[-1] == {'reason':'local_pumpfun_wait:token_and_window_wait',
        'stage':'entry_snapshot', 'retry_after_s':20.}
    now[0] += 20
    await ns['prepare']({**token, 'discovered_via':'shadow_followup'}, session)
    assert market.await_count == ns['enrich_token_birth'].await_count == 2
