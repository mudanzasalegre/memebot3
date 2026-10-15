"""Stop helper boundaries without provider/config/operational imports."""
import ast
import datetime as dt
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.local_stop import StopBinding, StopProtocolError
from scripts import request_runtime_stop as helper

ROOT = Path(__file__).resolve().parents[1]


def _owner(root):
    binding = StopBinding.create(root, pid=123)
    path = root / 'data/run_bot.lock'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(binding.owner_fields()))
    return binding, path


def test_replacement_owner_during_process_open_is_not_commanded(tmp_path):
    binding, path = _owner(tmp_path)
    replacement = StopBinding.create(tmp_path, pid=123)
    closed = []
    class Observer:
        def __init__(self, pid): path.write_text(json.dumps(replacement.owner_fields()))
        def __enter__(self): return self
        def __exit__(self, *exc): closed.append(True)
        def is_running(self, pid): raise AssertionError('Must not observe/stop replaced owner')
    with pytest.raises(StopProtocolError, match='changed'):
        helper.request_runtime_stop(tmp_path, expected_pid=123, observation_factory=Observer)
    assert closed == [True] and not binding.requested() and not replacement.requested()


def test_wrong_verified_pid_is_rejected_before_process_api_or_write(tmp_path):
    binding, path = _owner(tmp_path)
    def forbidden(pid): raise AssertionError('Must not open mismatched process')
    with pytest.raises(StopProtocolError):
        helper.request_runtime_stop(tmp_path, expected_pid=124, observation_factory=forbidden)
    assert not binding.path().exists()


def test_cli_does_not_report_success_for_exit_without_cleanup_receipt(monkeypatch, capsys):
    monkeypatch.setattr(helper, 'request_runtime_stop', lambda *args, **kwargs: {
        'pid':123, 'status':'owner_exited', 'requested':True,
        'owner_exited':True, 'cleanup_confirmed':False})
    assert helper.main(['--expected-pid','123']) == 1
    assert not json.loads(capsys.readouterr().out)['cleanup_confirmed']


def test_cli_refusal_is_generic_not_a_dump_of_owner_or_filesystem_errors(monkeypatch, capsys):
    def denied(*args, **kwargs): raise StopProtocolError('operator payload must not appear')
    monkeypatch.setattr(helper, 'request_runtime_stop', denied)
    assert helper.main(['--expected-pid','123']) == 2
    raw = capsys.readouterr().out
    assert 'operator payload' not in raw and json.loads(raw)['status'] == 'cooperative_unavailable'


def _native_lock_functions(tmp_path, monkeypatch):
    tree = ast.parse((ROOT / 'run_bot.py').read_text(encoding='utf-8'))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {'_acquire_process_lock', '_release_process_lock'}]
    class Lock:
        def __init__(self, path): self.path, self.is_acquired, self.payload = path, False, None
        def acquire(self, *, payload): self.is_acquired, self.payload = True, payload
        def release(self): self.is_acquired = False
    namespace = {'_PROCESS_LOCK':None, '_PROCESS_STOP_BINDING':None, 'PROJECT_ROOT':tmp_path,
                 'DRY_RUN':True, 'SingleInstanceLock':Lock, 'os':os,
                 'dt':dt, 'sys':SimpleNamespace(argv=['run_bot.py','--dry-run'])}
    exec(compile(ast.Module(body=functions, type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    return namespace


def test_actual_native_lock_functions_publish_fresh_binding_and_clear_only_owned_request(tmp_path, monkeypatch):
    native = _native_lock_functions(tmp_path, monkeypatch)
    native['_acquire_process_lock']()
    binding, lock = native['_PROCESS_STOP_BINDING'], native['_PROCESS_LOCK']
    assert StopBinding.from_owner(tmp_path, lock.payload) == binding
    assert lock.payload['pid'] == os.getpid() and lock.payload['dry_run'] is True
    native['_acquire_process_lock']()
    assert native['_PROCESS_STOP_BINDING'] is binding
    binding.request(); binding.complete()
    old = StopBinding.create(tmp_path); old.request()
    native['_release_process_lock']()
    assert not binding.requested() and binding.completed() and old.requested()
    assert not lock.is_acquired and native['_PROCESS_STOP_BINDING'] is None
    native['_acquire_process_lock']()
    assert native['_PROCESS_STOP_BINDING'].nonce != binding.nonce
    native['_release_process_lock']()


def test_failed_singleton_acquisition_does_not_install_or_request_a_binding(tmp_path, monkeypatch):
    native = _native_lock_functions(tmp_path, monkeypatch)
    class Busy:
        def __init__(self, *args): pass
        def acquire(self, **kwargs): raise RuntimeError('another owner')
    native['SingleInstanceLock'] = Busy
    with pytest.raises(RuntimeError, match='another owner'):
        native['_acquire_process_lock']()
    assert native['_PROCESS_LOCK'] is None and native['_PROCESS_STOP_BINDING'] is None
    assert not (tmp_path / 'data').exists()


def test_actual_main_acknowledges_only_after_asyncio_run_and_releases_lock_in_finally(tmp_path):
    tree = ast.parse((ROOT / 'run_bot.py').read_text(encoding='utf-8'))
    main = tree.body[-1]
    assert isinstance(main, ast.If)
    calls = []
    from runtime.local_stop import LocalStopRequested
    async def runtime(): raise AssertionError('AST fixture never enters operational runtime')
    def run(coro):
        coro.close()
        calls.append('drained')
        raise LocalStopRequested()
    namespace = {'__name__':'__main__', '_runner':runtime,
        '_acquire_process_lock':lambda: calls.append('acquired'),
        '_release_process_lock':lambda: calls.append('released'),
        '_PROCESS_STOP_BINDING':SimpleNamespace(complete=lambda: calls.append('acknowledged')),
        'asyncio':SimpleNamespace(run=run), 'SingleInstanceLockError':RuntimeError,
        'log':SimpleNamespace(info=lambda *args: calls.append('logged'))}
    exec(compile(ast.Module(body=[main], type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    assert calls == ['acquired','drained','acknowledged','logged','released']
