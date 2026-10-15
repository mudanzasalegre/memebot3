"""Run-scoped local stop intent and cleanup acknowledgment; never a kill primitive."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import math
import time
import uuid

from runtime.owned_thread import await_owned_thread
from runtime.loop_scheduler import supervise

VERSION = 'memebot-local-stop-v1'
MAX_BYTES = 4096


class StopProtocolError(RuntimeError):
    pass


class LocalStopRequested(Exception):
    """Expected termination, caught only after native supervisor cleanup."""


def _read_object(path):
    """Read only bounded regular files; never report untrusted contents."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > MAX_BYTES:
            return None
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


@dataclass(frozen=True)
class StopBinding:
    project_root: Path
    pid: int
    nonce: str

    def __post_init__(self):
        if type(self.pid) is not int or not 0 < self.pid <= 0xFFFFFFFF:
            raise StopProtocolError('Invalid stop owner PID')
        if not isinstance(self.nonce, str) or re.fullmatch(r'[0-9a-f]{32}', self.nonce) is None:
            raise StopProtocolError('Invalid stop generation')
        root = Path(self.project_root).resolve()
        if not root.is_dir():
            raise StopProtocolError('Stop project root is not a directory')
        object.__setattr__(self, 'project_root', root)
        self.path()  # Reject a pre-existing path escape before use.

    @classmethod
    def create(cls, project_root, *, pid=None):
        return cls(Path(project_root), os.getpid() if pid is None else pid, uuid.uuid4().hex)

    @classmethod
    def from_owner(cls, project_root, owner):
        if not isinstance(owner, dict) or owner.get('stop_protocol') != VERSION:
            raise StopProtocolError('Owner has no supported cooperative-stop binding')
        return cls(Path(project_root), owner.get('pid'), owner.get('stop_nonce'))

    def owner_fields(self):
        return {'pid': self.pid, 'stop_protocol': VERSION, 'stop_nonce': self.nonce}

    def _path(self, prefix):
        path = self.project_root / 'data' / 'runtime' / f'{prefix}-{self.nonce}.json'
        if path.is_symlink() or not path.resolve().is_relative_to(self.project_root):
            raise StopProtocolError('Stop request escapes the project')
        return path

    def path(self):
        return self._path('stop')

    def completion_path(self):
        return self._path('stopped')

    def _payload(self, *, completed=False):
        return {'protocol': VERSION, 'pid': self.pid, 'nonce': self.nonce,
                **({'completion': 'owned_cleanup_finished'} if completed else {'request': 'stop'})}

    def matches(self, payload):
        return self._matches(payload)

    def _matches(self, payload, *, completed=False):
        return (isinstance(payload, dict) and type(payload.get('pid')) is int
                and payload == self._payload(completed=completed))

    def requested(self):
        return self.matches(_read_object(self.path()))

    def completed(self):
        return self._matches(_read_object(self.completion_path()), completed=True)

    def request(self):
        return self._publish()

    def complete(self):
        """Caller may acknowledge only after native owned cleanup has drained."""
        return self._publish(completed=True)

    def _publish(self, *, completed=False):
        """Exclusive bounded publication; an unfinished frame is not a command.

        Never overwrite a conflicting operator artifact. Readers reject partial
        JSON until the complete matching request has been written and flushed.
        Repeated matching requests are idempotent for this generation only.
        """
        path = self.completion_path() if completed else self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path = self.completion_path() if completed else self.path()
        payload = self._payload(completed=completed)
        raw = json.dumps(payload, sort_keys=True).encode('utf-8')
        assert len(raw) < MAX_BYTES
        try:
            with path.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if not self._matches(_read_object(path), completed=completed):
                raise StopProtocolError('Conflicting stop request artifact') from None
        return path

    def clear_request(self):
        return self._clear()

    def clear_completion(self):
        return self._clear(completed=True)

    def _clear(self, *, completed=False):
        """Only remove a regular, exact matching protocol artifact, never a tree."""
        try:
            path = self.completion_path() if completed else self.path()
            before = path.stat() if path.exists() else None
            if before is None or not self._matches(_read_object(path), completed=completed):
                return False
            current = path.stat()
            if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (
                    current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size):
                return False
            path.unlink()
            return True
        except (OSError, StopProtocolError):
            return False


async def watch_local_stop(binding: StopBinding, *, interval=.25):
    while True:
        if await await_owned_thread(binding.requested):
            raise LocalStopRequested('Cooperative local stop requested')
        await asyncio.sleep(interval)


async def run_with_local_stop(runtime_factory, binding):
    """Keep expected stop separate from a real runtime/cleanup failure.

    The shared critical-loop supervisor retains its original failure priority.
    Only this expected-stop boundary inspects a drained runtime's real errors
    before allowing the caller to acknowledge cooperative cleanup.
    """
    errors = []
    async def owned_runtime():
        try:
            return await runtime_factory()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            errors.append(exc)
            raise
    try:
        await supervise([('native-runtime', owned_runtime()),
                         ('local-stop', watch_local_stop(binding))])
    except LocalStopRequested:
        if errors:
            raise errors[0] from None
        raise


def read_owner_binding(project_root, *, expected_pid=None):
    root = Path(project_root).resolve()
    path = root / 'data' / 'run_bot.lock'
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise StopProtocolError('Owner receipt escapes the project')
    binding = StopBinding.from_owner(root, _read_object(path))
    if expected_pid is not None and (type(expected_pid) is not int or expected_pid != binding.pid):
        raise StopProtocolError('Owner receipt does not match the verified runtime PID')
    return binding


def request_and_wait(binding, *, is_running, timeout=15., clock=time.monotonic, sleep=time.sleep):
    """Publish intent, then prove process exit; never kill or infer exit from IO.

    The caller verifies process scope/creation identity before constructing the
    binding and retains that identity for any separately authorized fallback.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 120:
        raise StopProtocolError('Invalid cooperative-stop timeout')
    started = clock()
    if type(started) not in (int, float) or not math.isfinite(started):
        raise StopProtocolError('Invalid cooperative-stop clock')
    def observe():
        try:
            value = is_running(binding.pid)
            return value if type(value) is bool else None
        except Exception:
            return None
    def result(status, *, requested, exited=False):
        # An intent or process disappearance alone is not proof of cleanup.
        confirmed = exited and binding.completed()
        if exited:
            binding.clear_request()
            if confirmed:
                binding.clear_completion()
        return {'pid': binding.pid, 'status': status, 'requested': requested,
                'owner_exited': exited, 'cleanup_confirmed': confirmed}
    alive = observe()
    if alive is None:
        return result('observation_failed', requested=False)
    if alive is False:
        return result('owner_not_running', requested=False, exited=True)
    binding.request()
    last = started
    while True:
        alive = observe()
        if alive is None:
            return result('observation_failed', requested=True)
        if alive is False:
            return result('owner_exited', requested=True, exited=True)
        now = clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < last:
            return result('observation_failed', requested=True)
        last = now
        if now - started >= timeout:
            return result('cooperative_timeout', requested=True)
        sleep(min(.1, timeout - (now - started)))
