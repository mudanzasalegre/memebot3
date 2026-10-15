"""Windows end-to-end stop script, confined to synthetic temporary workspaces.

The finalizer is a receipt-only stub here: actual SQLite behavior is covered by
test_finalize_stack_stop, not claimed from this provider-free process fixture.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from runtime.local_stop import StopBinding

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Native Windows stop_stack acceptance')


def _until(predicate, *, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'Synthetic fixture did not reach bounded milestone'
        time.sleep(.02)


def _environment():
    keep = {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA',
            'APPDATA', 'HOMEDRIVE', 'HOMEPATH', 'COMSPEC', 'PATHEXT'}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env


def _workspace(tmp_path):
    root = tmp_path / 'isolated stop workspace'
    root.mkdir()
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(root / '.venv')],
                   env=_environment(), capture_output=True, check=True, timeout=20,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    for relative in ('scripts/stop_stack.ps1', 'scripts/request_runtime_stop.py',
                     'runtime/__init__.py', 'runtime/local_stop.py', 'runtime/process_observation.py',
                     'runtime/owned_thread.py', 'runtime/loop_scheduler.py'):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    shutil.copy2(ROOT / 'tests/fixtures/local_stop_worker.py', root / 'run_bot.py')
    # Deliberately not the real finalizer, config, DB, providers or wallet.
    (root / 'scripts/finalize_stack_stop.py').write_text(
        "from pathlib import Path\nPath(__file__).resolve().parents[1].joinpath('finalizer-receipt').touch()\n")
    return root


def _launch(root, *, ignore_stop=False):
    return subprocess.Popen([sys.executable, str(root / 'run_bot.py'), '--root', str(root),
        '--source-root', str(ROOT), *(['--ignore-stop'] if ignore_stop else [])], env=_environment(),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)


def _dispose(process, root):
    (root / 'release').touch()
    try:
        owner = json.loads((root / 'owner.json').read_text())
        StopBinding.from_owner(root, owner).request()
    except (OSError, ValueError, RuntimeError):
        pass
    if process.poll() is None:
        try:
            process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()  # Exact still-owned synthetic launcher, never a discovered PID.
            process.communicate(timeout=5)


def _stop_command(root, *, force=False):
    return ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        str(root / 'scripts/stop_stack.ps1'), '-RequestedBy', 'provider-free-qa',
        *(['-Force'] if force else [])]


def test_normal_stack_stop_waits_for_owned_cleanup_without_taskkill_or_foreign_process_stop(tmp_path):
    root = _workspace(tmp_path)
    process = _launch(root)
    foreign = subprocess.Popen([sys.executable, '-I', '-c', 'import sys;sys.stdin.buffer.read(1)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    controller = None
    try:
        _until(lambda: (root / 'io-started').exists())
        assert process.poll() is None
        binding = StopBinding.from_owner(root, json.loads((root / 'owner.json').read_text()))
        old = StopBinding.create(root, pid=binding.pid)
        old.request()
        controller = subprocess.Popen(_stop_command(root), env=_environment(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        _until(binding.requested)
        assert controller.poll() is None and process.poll() is None
        assert not binding.completed() and not (root / 'cleanup-finished').exists()
        (root / 'release').touch()
        output = controller.communicate(timeout=25)
        assert controller.returncode == 0, output
        assert process.communicate(timeout=5) == (b'', b'')
        assert process.returncode == 0 and (root / 'stopped').is_file()
        log = (root / 'logs/stop_stack.log').read_text(encoding='utf-8-sig')
        assert '"cleanup_confirmed": true' in log and 'helper_exit_code=0' in log
        assert 'taskkill_result' not in log and 'stage=forced' not in log
        assert (root / 'finalizer-receipt').exists()
        assert not binding.path().exists() and not binding.completion_path().exists()
        assert old.path().exists(), 'Foreign generation must be preserved'
        assert foreign.poll() is None, 'Unrelated process must remain untouched'
    finally:
        _dispose(process, root)
        foreign.communicate(input=b'x', timeout=5)
        if controller is not None and controller.poll() is None:
            controller.communicate(timeout=25)


@pytest.mark.parametrize('force', [False, True])
def test_unresponsive_owner_retains_verified_fallback_and_force_first(tmp_path, force):
    root = _workspace(tmp_path)
    process = _launch(root, ignore_stop=True)
    try:
        _until(lambda: (root / 'io-started').exists())
        started = time.monotonic()
        result = subprocess.run(_stop_command(root, force=force), env=_environment(),
            capture_output=True, timeout=35, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, (result.stdout, result.stderr)
        process.communicate(timeout=5)
        log = (root / 'logs/stop_stack.log').read_text(encoding='utf-8-sig')
        assert 'taskkill_result' in log and not (root / 'stopped').exists()
        assert '"cleanup_confirmed": true' not in log
        if force:
            assert 'cooperative_stop requesting' not in log and 'stage=forced' in log
        else:
            assert 'cooperative_stop requesting' in log
            assert '"status": "cooperative_timeout"' in log
            assert time.monotonic() - started >= 15
        assert (root / 'finalizer-receipt').exists()
    finally:
        _dispose(process, root)


def test_hung_request_helper_cannot_block_verified_stack_fallback_indefinitely(tmp_path):
    root = _workspace(tmp_path)
    # Only this copied helper is replaced. It imports no bot/config/provider.
    (root / 'scripts/request_runtime_stop.py').write_text(
        "from pathlib import Path\nimport time\n"
        "Path(__file__).resolve().parents[1].joinpath('hung-helper-started').touch()\ntime.sleep(60)\n")
    process = _launch(root, ignore_stop=True)
    try:
        _until(lambda: (root / 'io-started').exists())
        started = time.monotonic()
        result = subprocess.run(_stop_command(root), env=_environment(),
            capture_output=True, timeout=40, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, (result.stdout, result.stderr)
        process.communicate(timeout=5)
        elapsed = time.monotonic() - started
        log = (root / 'logs/stop_stack.log').read_text(encoding='utf-8-sig')
        assert (root / 'hung-helper-started').exists()
        assert 'helper_outer_timeout_seconds=20' in log
        assert 'stage=cooperative_helper_timeout' in log and 'taskkill_result' in log
        assert '"cleanup_confirmed": true' not in log and not (root / 'stopped').exists()
        assert elapsed >= 20 and (root / 'finalizer-receipt').exists()
    finally:
        _dispose(process, root)
