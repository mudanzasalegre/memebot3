"""Test-only controller phase clocks; no process, provider or actual stop."""
from types import SimpleNamespace

import pytest

import test_stop_stack_cooperative as native


def fixture_values(tmp_path):
    return (tmp_path, SimpleNamespace(requested=lambda: True),
        SimpleNamespace(poll=lambda: None), SimpleNamespace(poll=lambda: None))


def test_startup_and_delivery_use_distinct_bounded_phase_budgets(monkeypatch, tmp_path):
    observed = []
    monkeypatch.setattr(native, '_until', lambda predicate, timeout: observed.append(timeout))
    result = native._await_owned_stop_request(*fixture_values(tmp_path))
    assert observed == [90, 30]
    assert [item['phase'] for item in result] == ['controller_startup', 'owned_request_delivery']
    assert all(item['elapsed_s'] >= 0 for item in result)


@pytest.mark.parametrize('failed_phase', ['controller_startup', 'owned_request_delivery'])
def test_failure_keeps_its_phase_timeout_and_actual_process_log_diagnostics(monkeypatch, tmp_path, failed_phase):
    observed = []
    if failed_phase == 'owned_request_delivery':
        path = tmp_path / 'logs/stop_stack.log'
        path.parent.mkdir()
        path.write_text('stop_stack requested_by=fixture\ncooperative_phase=owner_read_begin\n')
    def bounded_failure(predicate, timeout):
        observed.append(timeout)
        if len(observed) == (1 if failed_phase == 'controller_startup' else 2):
            raise AssertionError('original bounded milestone')
    monkeypatch.setattr(native, '_until', bounded_failure)
    with pytest.raises(AssertionError, match='phase=' + failed_phase) as captured:
        native._await_owned_stop_request(*fixture_values(tmp_path))
    message = str(captured.value)
    assert 'controller_exit=None owner_exit=None' in message
    assert 'phase_elapsed_s=' in message and 'total_elapsed_s=' in message
    assert ('log=<no log>' in message if failed_phase == 'controller_startup'
            else 'cooperative_phase=owner_read_begin' in message)
    assert observed == ([90] if failed_phase == 'controller_startup' else [90, 30])
    assert str(captured.value.__cause__) == 'original bounded milestone'


def test_delivery_cannot_be_satisfied_by_controller_startup_alone(monkeypatch, tmp_path):
    path = tmp_path / 'logs/stop_stack.log'
    path.parent.mkdir()
    path.write_text('stop_stack requested_by=fixture\n')
    root, _, controller, owner = fixture_values(tmp_path)
    outcomes = []
    def inspect(predicate, timeout):
        outcomes.append(predicate())
        if timeout == 30:
            raise AssertionError('No matching request')
    monkeypatch.setattr(native, '_until', inspect)
    with pytest.raises(AssertionError, match='owned_request_delivery'):
        native._await_owned_stop_request(root, SimpleNamespace(requested=lambda: False), controller, owner)
    assert outcomes == [True, False]
