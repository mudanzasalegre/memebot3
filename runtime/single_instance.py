from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path
from typing import Any

if os.name == "nt":  # pragma: no cover - Windows runtime path
    import ctypes
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - POSIX fallback
    import fcntl


class SingleInstanceLockError(RuntimeError):
    """Raised when another live process already owns the same lock."""


class SingleInstanceLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle = None
        self._mutex_handle = None

    @property
    def is_acquired(self) -> bool:
        return self._handle is not None

    def acquire(self, *, payload: dict[str, Any] | None = None) -> None:
        if self._handle is not None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)

        if os.name == "nt":
            try:
                self._acquire_windows_mutex()
            except OSError as exc:
                owner = self._read_owner_from_path()
                detail = f"owner={owner}" if owner else "owner=unknown"
                raise SingleInstanceLockError(f"lock busy at {self.path} ({detail})") from exc

            handle = self.path.open("a+", encoding="utf-8")
            self._handle = handle
            self._write_payload(payload or {})
            return

        handle = self.path.open("a+", encoding="utf-8")
        try:
            handle.seek(0)
            if self.path.stat().st_size == 0:
                handle.write(" ")
                handle.flush()
            handle.seek(0)
            self._lock_handle(handle)
        except OSError as exc:
            owner = self._read_owner(handle)
            handle.close()
            detail = f"owner={owner}" if owner else "owner=unknown"
            raise SingleInstanceLockError(f"lock busy at {self.path} ({detail})") from exc

        self._handle = handle
        self._write_payload(payload or {})

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return

        try:
            self._write_payload({})
        except Exception:
            pass

        try:
            if os.name != "nt":
                handle.seek(0)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                handle.close()
            finally:
                self._handle = None

        if os.name == "nt" and self._mutex_handle is not None:
            try:
                _kernel32.ReleaseMutex(self._mutex_handle)
            except Exception:
                pass
            try:
                _kernel32.CloseHandle(self._mutex_handle)
            except Exception:
                pass
            self._mutex_handle = None

    def _write_payload(self, payload: dict[str, Any]) -> None:
        if self._handle is None:
            return
        body = json.dumps(payload, ensure_ascii=True, sort_keys=True) if payload else "{}"
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(body)
        self._handle.flush()

    @staticmethod
    def _read_owner(handle) -> dict[str, Any] | str | None:
        try:
            handle.seek(0)
            raw = handle.read().strip()
        except Exception:
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw

    def _read_owner_from_path(self) -> dict[str, Any] | str | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open("r", encoding="utf-8", errors="ignore") as handle:
                return self._read_owner(handle)
        except Exception:
            return None

    @staticmethod
    def _lock_handle(handle) -> None:
        handle.seek(0)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _acquire_windows_mutex(self) -> None:
        name = self._windows_mutex_name()
        handle = _kernel32.CreateMutexW(None, True, name)
        if not handle:
            raise OSError("CreateMutexW failed")
        last_error = ctypes.get_last_error()
        if last_error == 183:  # ERROR_ALREADY_EXISTS
            _kernel32.CloseHandle(handle)
            raise OSError("mutex already exists")
        self._mutex_handle = handle

    def _windows_mutex_name(self) -> str:
        raw = str(self.path.resolve()).lower().encode("utf-8", errors="ignore")
        digest = hashlib.md5(raw).hexdigest()
        return f"Local\\memebot3_{digest}"
