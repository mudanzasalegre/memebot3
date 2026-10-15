"""Temporary requests and provider-free fixtures; no operational bot or wallet."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import subprocess
import time

import pytest

from runtime import local_stop as stop


def test_fresh_generation_is_bound_to_this_root_and_pid(tmp_path):
    a = stop.StopBinding.create(tmp_path)
    b = stop.StopBinding.create(tmp_path)
    assert a.nonce != b.nonce and a.pid == os.getpid()
    assert stop.StopBinding.from_owner(tmp_path, a.owner_fields()) == a
    assert not a.requested()
    a.request()
    assert a.requested() and not b.requested()
    assert a.clear_request() and not a.requested()


@pytest.mark.parametrize('pid', [None, True, 0, -1, 1.5, '123', 0x100000000])
def test_invalid_pid_is_not_a_binding(tmp_path, pid):
    with pytest.raises(stop.StopProtocolError):
        stop.StopBinding(tmp_path, pid, 'a' * 32)


@pytest.mark.parametrize('nonce', [None, True, '', 'A' * 32, 'a' * 31, '../outside', 'a' * 33])
def test_invalid_nonce_cannot_name_a_request(tmp_path, nonce):
    with pytest.raises(stop.StopProtocolError):
        stop.StopBinding(tmp_path, 123, nonce)


@pytest.mark.parametrize('change', ['pid', 'nonce', 'protocol', 'request', 'boolean-pid', 'array', 'partial', 'oversized', 'deep', 'extra'])
def test_stale_invalid_partial_or_oversized_requests_are_ignored_and_preserved(tmp_path, change):
    binding = stop.StopBinding.create(tmp_path)
    path = binding.path()
    path.parent.mkdir(parents=True)
    payload = {'protocol': stop.VERSION, 'pid': binding.pid, 'nonce': binding.nonce, 'request': 'stop'}
    if change in payload:
        payload[change] = 999 if change == 'pid' else 'wrong'
    elif change == 'boolean-pid': payload['pid'] = True
    elif change == 'extra': payload['foreign'] = 'operator-data'
    if change == 'array': payload = []
    raw = ('{' if change == 'partial' else 'x' * (stop.MAX_BYTES + 1) if change == 'oversized' else
           '[' * 1100 + '0' + ']' * 1100 if change == 'deep' else json.dumps(payload))
    path.write_text(raw)
    assert not binding.requested() and not binding.clear_request()
    assert path.read_text() == raw
    with pytest.raises(stop.StopProtocolError): binding.request()
    assert path.read_text() == raw


def test_same_pid_different_start_cannot_accept_an_old_request(tmp_path):
    old = stop.StopBinding.create(tmp_path, pid=123)
    new = stop.StopBinding.create(tmp_path, pid=123)
    old.request()
    assert old.requested() and not new.requested()
    assert not new.clear_request() and old.path().is_file()


def test_repeated_request_is_idempotent_without_rewriting_or_consuming_foreign_data(tmp_path):
    binding = stop.StopBinding.create(tmp_path)
    binding.request()
    before = binding.path().stat()
    binding.request()
    assert binding.path().stat().st_mtime_ns == before.st_mtime_ns
    foreign = binding.path().parent / 'operator.json'
    foreign.write_text('keep')
    assert binding.clear_request() and foreign.read_text() == 'keep'


def test_request_does_not_create_a_missing_root(tmp_path):
    root = tmp_path / 'absent'
    with pytest.raises(stop.StopProtocolError): stop.StopBinding.create(root)
    assert not root.exists()


def test_cleanup_io_failure_cannot_turn_a_completed_stop_into_another_exception(tmp_path, monkeypatch):
    binding = stop.StopBinding.create(tmp_path)
    binding.request()
    def blocked(*args, **kwargs): raise PermissionError('synthetic read-only directory')
    monkeypatch.setattr(Path, 'unlink', blocked)
    assert not binding.clear_request()


def test_owner_receipt_must_match_the_verified_runtime_pid(tmp_path):
    binding = stop.StopBinding.create(tmp_path, pid=123)
    path = tmp_path / 'data' / 'run_bot.lock'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(binding.owner_fields()))
    assert stop.read_owner_binding(tmp_path, expected_pid=123) == binding
    with pytest.raises(stop.StopProtocolError): stop.read_owner_binding(tmp_path, expected_pid=124)
    assert not binding.path().exists()


def test_controller_needs_actual_owner_exit_not_only_an_intent_file(tmp_path):
    binding = stop.StopBinding.create(tmp_path, pid=123)
    clock = [1000.]
    observed = []
    def sleep(seconds):
        assert binding.requested()
        observed.append(seconds)
        clock[0] += seconds
        if clock[0] >= 1000.25:
            binding.complete()
    result = stop.request_and_wait(binding, is_running=lambda pid: clock[0] < 1000.25,
                                   clock=lambda: clock[0], sleep=sleep)
    assert result == {'pid':123, 'status':'owner_exited', 'requested':True,
                      'owner_exited':True, 'cleanup_confirmed':True}
    assert observed and not binding.path().exists() and not binding.completion_path().exists()


def test_disappearance_without_completion_does_not_certify_cleanup(tmp_path):
    binding = stop.StopBinding.create(tmp_path)
    observations = iter([True, False])
    result = stop.request_and_wait(binding, is_running=lambda pid: next(observations))
    assert result['owner_exited'] and not result['cleanup_confirmed']
    assert not binding.path().exists()


def test_completion_without_process_exit_is_not_a_completed_stop(tmp_path):
    binding = stop.StopBinding.create(tmp_path)
    binding.complete()
    result = stop.request_and_wait(binding, is_running=lambda pid: True, timeout=0)
    assert not result['owner_exited'] and not result['cleanup_confirmed']
    assert binding.completed(), 'Do not consume acknowledgment before the original process exits'


@pytest.mark.parametrize('change', ['pid', 'nonce', 'protocol', 'completion', 'extra', 'boolean-pid'])
def test_foreign_completion_is_neither_trusted_nor_removed(tmp_path, change):
    binding = stop.StopBinding.create(tmp_path)
    path = binding.completion_path()
    path.parent.mkdir(parents=True)
    payload = binding._payload(completed=True)
    if change in payload:
        payload[change] = 999 if change == 'pid' else 'wrong'
    elif change == 'boolean-pid': payload['pid'] = True
    else: payload['foreign'] = 'operator-data'
    raw = json.dumps(payload)
    path.write_text(raw)
    assert not binding.completed() and not binding.clear_completion()
    result = stop.request_and_wait(binding, is_running=lambda pid: False)
    assert result['owner_exited'] and not result['cleanup_confirmed']
    assert path.read_text() == raw


def test_completion_is_distinct_and_idempotent_for_only_its_generation(tmp_path):
    binding = stop.StopBinding.create(tmp_path)
    binding.complete()
    before = binding.completion_path().stat().st_mtime_ns
    binding.complete()
    assert binding.completed() and not binding.requested()
    assert binding.completion_path().stat().st_mtime_ns == before
    assert binding.clear_completion() and not binding.completed()


def test_controller_timeout_does_not_claim_exit_or_delete_pending_intent(tmp_path):
    binding = stop.StopBinding.create(tmp_path, pid=123)
    clock = [1000.]
    def sleep(seconds): clock[0] += seconds
    result = stop.request_and_wait(binding, is_running=lambda pid: True, timeout=.2,
                                   clock=lambda: clock[0], sleep=sleep)
    assert result['status'] == 'cooperative_timeout' and not result['owner_exited']
    assert binding.requested()


def test_missing_process_causes_no_request_write(tmp_path):
    binding = stop.StopBinding.create(tmp_path, pid=123)
    result = stop.request_and_wait(binding, is_running=lambda pid: False)
    assert result['owner_exited'] and not result['requested'] and not binding.path().exists()


@pytest.mark.parametrize('timeout', [True, None, float('nan'), float('inf'), -1, 121])
def test_invalid_timeout_cannot_publish_a_stop_intent(tmp_path, timeout):
    binding = stop.StopBinding.create(tmp_path)
    with pytest.raises(stop.StopProtocolError):
        stop.request_and_wait(binding, is_running=lambda pid: True, timeout=timeout)
    assert not binding.path().exists()


@pytest.mark.parametrize('clock_value', [True, None, float('nan'), float('inf'), 999.])
def test_controller_clock_failure_never_invents_process_exit(tmp_path, clock_value):
    binding = stop.StopBinding.create(tmp_path)
    calls = iter([1000., clock_value])
    result = stop.request_and_wait(binding, is_running=lambda pid: True, clock=lambda: next(calls))
    assert result['status'] == 'observation_failed' and not result['owner_exited']
    assert binding.requested()


def test_a_path_escape_cannot_be_read_written_or_removed(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'; root.mkdir()
    outside = tmp_path / 'outside'
    binding = stop.StopBinding.create(root)
    original = Path.resolve
    def redirected(path, *args, **kwargs):
        if path.name.startswith('stop-'):
            return outside / path.name
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', redirected)
    with pytest.raises(stop.StopProtocolError): binding.requested()
    with pytest.raises(stop.StopProtocolError): binding.request()
    assert not binding.clear_request() and not outside.exists()


@pytest.mark.parametrize('observation', [None, 0, 1, [], 'stopped'])
def test_ambiguous_process_observation_is_not_exit(tmp_path, observation):
    binding = stop.StopBinding.create(tmp_path)
    result = stop.request_and_wait(binding, is_running=lambda pid: observation)
    assert result['status'] == 'observation_failed' and not result['owner_exited']
    assert not binding.path().exists()


def test_process_observation_error_does_not_become_a_success_or_a_kill(tmp_path):
    binding = stop.StopBinding.create(tmp_path)
    def denied(pid): raise PermissionError('no process observation')
    result = stop.request_and_wait(binding, is_running=denied)
    assert result['status'] == 'observation_failed' and not result['owner_exited']


@pytest.mark.asyncio
async def test_expected_stop_does_not_hide_a_real_owned_cleanup_failure(tmp_path):
    binding = stop.StopBinding.create(tmp_path)
    entered = asyncio.Event()
    async def broken_runtime():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            raise RuntimeError('owned cleanup failed')
    owner = asyncio.create_task(stop.run_with_local_stop(broken_runtime, binding))
    await entered.wait()
    binding.request()
    with pytest.raises(RuntimeError, match='owned cleanup failed'):
        await owner


@pytest.mark.asyncio
async def test_runtime_error_without_request_is_not_reclassified_as_operator_stop(tmp_path):
    binding = stop.StopBinding.create(tmp_path)
    async def broken_runtime(): raise ValueError('original native failure')
    with pytest.raises(ValueError, match='original native failure'):
        await stop.run_with_local_stop(broken_runtime, binding)


@pytest.mark.asyncio
async def test_repeated_parent_cancellation_does_not_abandon_native_cleanup_or_acknowledge(tmp_path):
    binding = stop.StopBinding.create(tmp_path)
    entered, draining, release, finished = (asyncio.Event() for _ in range(4))
    async def runtime():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
            finished.set()
    owner = asyncio.create_task(stop.run_with_local_stop(runtime, binding))
    try:
        await entered.wait()
        binding.request()
        await asyncio.wait_for(draining.wait(), 5)
        for _ in range(2):
            owner.cancel()
            await asyncio.sleep(.01)
            assert not owner.done() and not finished.is_set() and not binding.completed()
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
    assert owner.cancelled() and finished.is_set() and not binding.completed()


@pytest.mark.asyncio
async def test_stop_exception_reaches_caller_only_after_native_supervisor_drains_worker(tmp_path):
    from runtime.loop_scheduler import supervise
    binding = stop.StopBinding.create(tmp_path)
    entered, draining, release, finished = (asyncio.Event() for _ in range(4))
    async def owner():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
            finished.set()
    runtime = asyncio.create_task(supervise([('owned-runtime', owner()),
        ('local-stop', stop.watch_local_stop(binding, interval=.001))]))
    try:
        await entered.wait()
        binding.request()
        await asyncio.wait_for(draining.wait(), 5)
        assert not runtime.done() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(stop.LocalStopRequested): await runtime
    assert finished.is_set() and binding.clear_request()


@pytest.mark.asyncio
async def test_watcher_does_not_abandon_a_filesystem_read_on_repeated_cancel(tmp_path, monkeypatch):
    binding = stop.StopBinding.create(tmp_path)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def read():
        entered.set()
        try:
            assert release.wait(10)
            return False
        finally: finished.set()
    monkeypatch.setattr(stop.StopBinding, 'requested', lambda self: read())
    watcher = asyncio.create_task(stop.watch_local_stop(binding))
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while not entered.is_set():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(.001)
        watcher.cancel()
        await asyncio.sleep(.01)
        watcher.cancel()
        await asyncio.sleep(.01)
        assert not watcher.done() and not finished.is_set()
    finally:
        release.set()
        await asyncio.gather(watcher, return_exceptions=True)
    assert watcher.cancelled() and finished.is_set()


@pytest.mark.parametrize('initial_request', ['matching', 'old-generation'])
def test_real_provider_free_subprocess_exits_only_after_its_owned_io_finishes(tmp_path, initial_request):
    keep = {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA',
            'APPDATA', 'HOMEDRIVE', 'HOMEPATH', 'COMSPEC', 'PATHEXT'}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    process = subprocess.Popen([sys.executable, str(Path(__file__).parent / 'fixtures' / 'local_stop_worker.py'),
        '--root', str(tmp_path), '--source-root', str(Path(__file__).resolve().parents[1])], env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / 'io-started').exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline
            time.sleep(.01)
        binding = stop.StopBinding.from_owner(tmp_path, json.loads((tmp_path / 'owner.json').read_text()))
        if binding.pid != process.pid:
            assert os.name == 'nt' and process.poll() is None
            # Windows venv launches a real interpreter child. Verify this live
            # relationship, not equality with the launcher's Popen PID.
            metadata = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                f"Get-CimInstance Win32_Process -Filter 'ProcessId={binding.pid}' | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"],
                capture_output=True, text=True, check=True, timeout=5)
            child = json.loads(metadata.stdout)
            assert child['ProcessId'] == binding.pid and child['ParentProcessId'] == process.pid
            assert 'local_stop_worker.py' in child['CommandLine'] and str(tmp_path) in child['CommandLine']
        if initial_request == 'old-generation':
            old = stop.StopBinding.create(tmp_path, pid=binding.pid)
            old.request()
            time.sleep(.1)
            assert process.poll() is None and not binding.requested()
        binding.request()
        binding.request()
        time.sleep(.15)
        assert process.poll() is None and not (tmp_path / 'stopped').exists()
        assert not (tmp_path / 'io-finished').exists()
        (tmp_path / 'release').write_text('allow-owned-completion')
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        assert (tmp_path / 'stopped').read_text() == 'after-owned-cleanup'
        assert binding.completed()
        assert not binding.path().exists()
        if initial_request == 'old-generation':
            assert old.path().exists(), 'New owner deleted a foreign generation request'
    finally:
        (tmp_path / 'release').touch()
        try:
            owner = json.loads((tmp_path / 'owner.json').read_text())
            stop.StopBinding.from_owner(tmp_path, owner).request()
        except (OSError, ValueError, stop.StopProtocolError):
            pass
        if process.poll() is None:
            try: process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()  # Exact still-owned provider-free fixture handle.
                process.communicate(timeout=5)
