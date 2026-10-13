from __future__ import annotations

import json
import os
import errno
import math
import time
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import BinaryIO
from uuid import uuid4

from utils.atomic_json import write_json_atomic

from config.config import PROJECT_ROOT

LOCK_PATH = PROJECT_ROOT / "data" / "metrics" / "training.lock"
VERSION = "os_owned_training_cycle_v1"


@dataclass
class _Owner:
    handle: BinaryIO
    pid: int
    thread: threading.Thread
    token: str


_owners: dict[str, _Owner] = {}
_gate = threading.RLock()


def _path_key(path: Path) -> tuple[Path, str]:
    resolved = Path(path).resolve()
    return resolved, os.path.normcase(str(resolved))


def _pid_is_running(pid: int) -> bool:
    """Legacy migration only; an inaccessible owner is not proved stopped."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # invalid PID; access denied remains busy
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _legacy_owner_is_absent(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    if not payload or payload.get("version") == VERSION:
        # The stable OS lock, not this diagnostic JSON or its PID, is authority.
        return True
    if "version" in payload:
        return False
    pid, created = payload.get("pid"), payload.get("created_epoch_s")
    if (type(pid) is not int or not 0 < pid < 2**32
            or isinstance(created, bool) or not isinstance(created, (int, float))
            or not math.isfinite(created) or created <= 0):
        return False
    return not _pid_is_running(pid)


def _lock_handle(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def acquire_lock(path: Path = LOCK_PATH, *, ttl_s: int = 1800) -> bool:
    """Nonblocking cycle ownership. TTL is diagnostic, never a live-owner lease."""
    if type(ttl_s) is not int or ttl_s <= 0:
        raise ValueError("training lock TTL must be a positive integer")
    path, key = _path_key(path)
    with _gate:
        previous = _owners.get(key)
        if previous is not None:
            if previous.pid == os.getpid():
                return False
            # A fork inherits descriptors, not permission to release its parent.
            previous.handle.close()
            del _owners[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        guard = path.with_name("." + path.name + ".guard")
        handle = guard.open("a+b")
        try:
            if not guard.stat().st_size:
                handle.write(b"\0")
                handle.flush()
            _lock_handle(handle)
        except OSError:
            handle.close()
            return False
        try:
            if not _legacy_owner_is_absent(path):
                _unlock_handle(handle)
                return False
            owner = _Owner(handle, os.getpid(), threading.current_thread(), uuid4().hex)
            write_json_atomic(path, {"version": VERSION, "pid": owner.pid,
                "owner_id": owner.token, "created_epoch_s": time.time(), "ttl_s": ttl_s})
            _owners[key] = owner
            return True
        except BaseException:
            if not handle.closed:
                _unlock_handle(handle)
            raise


def release_lock(path: Path = LOCK_PATH) -> None:
    """Only the acquiring process and thread may clear diagnostic ownership."""
    path, key = _path_key(path)
    with _gate:
        owner = _owners.get(key)
        if owner is None or owner.pid != os.getpid() or owner.thread is not threading.current_thread():
            return
        try:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("owner_id") == owner.token:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        finally:
            del _owners[key]
            _unlock_handle(owner.handle)
        # Never unlink the guard: a stable inode prevents split POSIX owners.


__all__ = ["LOCK_PATH", "acquire_lock", "release_lock"]
