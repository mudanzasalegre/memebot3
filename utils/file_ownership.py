"""Stable OS-owned file guards; marker age is never live-owner authority."""
from contextlib import contextmanager
import errno
import math
import os
from pathlib import Path
import time


def process_is_running(pid: int) -> bool:
    """Read-only legacy migration probe; inaccessible owners remain live."""
    if type(pid) is not int or not 0 < pid < 2**32:
        raise ValueError("Expected a positive process ID")
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
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError as error:
        return error.errno != errno.ESRCH
    return True


def _lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_file_guard(path: Path, *, deadline: float, poll_seconds: float):
    """Hold an opened stable inode through work and cleanup; never unlink it."""
    for name, value in (("deadline", deadline), ("poll_seconds", poll_seconds)):
        try:
            valid = type(value) in {int, float} and math.isfinite(float(value))
        except (OverflowError, ValueError):
            valid = False
        if not valid or name == "poll_seconds" and value <= 0:
            raise ValueError(f"Invalid file ownership {name}")
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        while not acquired:
            try:
                _lock(handle)
                acquired = True
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EBUSY, errno.EDEADLK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Timed out waiting for file ownership: {path}") from error
                time.sleep(min(poll_seconds, remaining))
        yield
    finally:
        try:
            if acquired:
                _unlock(handle)
        finally:
            handle.close()
