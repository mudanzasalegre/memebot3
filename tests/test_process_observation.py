"""Pure/temporary process observer tests; no operational process is started."""
import os
import subprocess
import sys

import pytest

from runtime import process_observation as observer


class FakeWindows:
    def __init__(self, *, handle=0x100000002, state=258):
        self.handle, self.state, self.calls = handle, state, []
    def OpenProcess(self, access, inherit, pid):
        self.calls.append(('open', access, inherit, pid))
        return self.handle
    def WaitForSingleObject(self, handle, timeout):
        self.calls.append(('wait', handle, timeout))
        return self.state
    def CloseHandle(self, handle):
        self.calls.append(('close', handle))
        return 1


@pytest.mark.parametrize(('state', 'expected'), [(0, False), (258, True), (0xFFFFFFFF, None), (128, None)])
def test_only_signaled_original_process_proves_exit(state, expected):
    api = FakeWindows(state=state)
    with observer.ProcessObservation(123, _api=api) as process:
        assert process.is_running(123) is expected
        assert process.is_running(124) is None and process.is_running(True) is None
    assert api.calls == [('open', 0x00100000, False, 123), ('wait', api.handle, 0), ('close', api.handle)]
    process.close()
    assert process.is_running(123) is None
    assert len(api.calls) == 3


def test_failed_open_cannot_be_reported_as_exit():
    api = FakeWindows(handle=0)
    with pytest.raises(observer.ProcessObservationError):
        observer.ProcessObservation(123, _api=api)
    assert api.calls == [('open', 0x00100000, False, 123)]


def test_windows_held_handle_is_not_reopened_on_each_observation():
    api = FakeWindows()
    with observer.ProcessObservation(123, _api=api) as process:
        assert process.is_running(123) is True
        api.state = 0
        assert process.is_running(123) is False
        assert process.is_running(123) is False
    assert sum(call[0] == 'open' for call in api.calls) == 1
    assert sum(call[0] == 'close' for call in api.calls) == 1


@pytest.mark.parametrize('pid', [True, None, 0, -1, 2**32, '123'])
def test_bad_pid_does_not_reach_the_process_api(pid):
    api = FakeWindows()
    with pytest.raises(observer.ProcessObservationError):
        observer.ProcessObservation(pid, _api=api)
    assert not api.calls


def test_actual_current_process_can_be_observed_but_never_terminated():
    with observer.ProcessObservation(os.getpid()) as process:
        assert process.is_running(os.getpid()) is True


@pytest.mark.skipif(os.name != 'nt', reason='Windows native process-handle acceptance')
@pytest.mark.parametrize('exit_code', [0, 1, 259])
def test_real_windows_handle_observes_even_exit_code_259_without_inventing_liveness(exit_code):
    # The venv launcher is deliberately observed here: it exits with its child.
    process = subprocess.Popen([sys.executable, '-I', '-c',
        f'import sys; sys.stdin.buffer.read(1); sys.exit({exit_code})'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with observer.ProcessObservation(process.pid) as handle:
            assert handle.is_running(process.pid) is True
            process.communicate(input=b'x', timeout=5)
            assert handle.is_running(process.pid) is False
    finally:
        if process.poll() is None:
            process.kill()  # Exact still-owned synthetic fixture handle only.
            process.communicate(timeout=5)
