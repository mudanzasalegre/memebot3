"""Read-only process-exit observation; never grants termination authority.

Windows contracts: Microsoft Learn OpenProcess and WaitForSingleObject.
Use SYNCHRONIZE only, hold the original object, and reject ambiguous wait states.
"""
from __future__ import annotations

import os


class ProcessObservationError(RuntimeError):
    pass


def _windows_api():
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    return api


class ProcessObservation:
    """Hold the original Windows process object while waiting for its exit.

    OpenProcess failure and unknown wait states are unknown, not proof of exit.
    The held handle avoids replacing an observed process with a reused PID.
    POSIX zero-signal observation is conservative (zombies may time out).
    """
    def __init__(self, pid, *, _api=None):
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
            raise ProcessObservationError('Invalid process PID')
        self.pid, self._api, self._handle = pid, None, None
        if os.name == 'nt' or _api is not None:
            self._api = _api if _api is not None else _windows_api()
            self._handle = self._api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
            if not self._handle:
                raise ProcessObservationError('Cannot observe original process')
        self._closed = False

    def is_running(self, pid):
        if self._closed or type(pid) is not int or pid != self.pid:
            return None
        if self._api is not None:
            state = self._api.WaitForSingleObject(self._handle, 0)
            if state == 0:  # WAIT_OBJECT_0: original process object signaled
                return False
            if state == 258:  # WAIT_TIMEOUT: original process not signaled
                return True
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        return True

    def close(self):
        if not self._closed:
            self._closed = True
            if self._handle is not None:
                self._api.CloseHandle(self._handle)
                self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
