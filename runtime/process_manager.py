from __future__ import annotations

import contextlib
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any


STATE_FILENAME = "ui_managed_bot_process.json"
CONSOLE_LOG_FILENAME = "ui_managed_bot.console.log"
WINDOWS_STILL_ACTIVE = 259


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def bot_process_state_path(project_root: Path) -> Path:
    return (Path(project_root).resolve() / "data" / "runtime" / STATE_FILENAME).resolve()


def bot_process_console_log_path(project_root: Path) -> Path:
    return (Path(project_root).resolve() / "logs" / CONSOLE_LOG_FILENAME).resolve()


def resolve_bot_python(project_root: Path) -> Path:
    project_root = Path(project_root).resolve()
    candidates = []
    if os.name == "nt":
        candidates.append(project_root / ".venv" / "Scripts" / "python.exe")
    else:
        candidates.append(project_root / ".venv" / "bin" / "python")
    candidates.append(Path(sys.executable).resolve())
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError("No Python executable found for UI-managed bot start")


def load_managed_bot_state(state_path: Path) -> dict[str, Any] | None:
    if not Path(state_path).exists():
        return None
    try:
        payload = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def save_managed_bot_state(state_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def clear_managed_bot_state(state_path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        Path(state_path).unlink()


def _is_windows_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    kernel32 = ctypes.windll.kernel32
    process = kernel32.OpenProcess(0x1000, False, int(pid))
    if not process:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)) == 0:
            return False
        return int(exit_code.value) == WINDOWS_STILL_ACTIVE
    finally:
        kernel32.CloseHandle(process)


def is_pid_running(pid: int | None) -> bool:
    if pid in (None, 0):
        return False
    normalized = int(pid)
    if normalized <= 0:
        return False
    if os.name == "nt":
        return _is_windows_pid_running(normalized)
    try:
        os.kill(normalized, 0)
    except OSError:
        return False
    return True


def start_managed_bot_process(
    project_root: Path,
    *,
    requested_by: str,
    requested_from: str = "ui",
    dry_run: bool = True,
    file_log: bool = True,
) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    state_path = bot_process_state_path(project_root)
    existing = load_managed_bot_state(state_path)
    existing_pid = int(existing.get("pid") or 0) if isinstance(existing, dict) else 0
    if existing_pid and is_pid_running(existing_pid):
        raise RuntimeError(f"UI-managed bot is already running with pid={existing_pid}")
    if existing_pid:
        clear_managed_bot_state(state_path)

    python_path = resolve_bot_python(project_root)
    console_log_path = bot_process_console_log_path(project_root)
    console_log_path.parent.mkdir(parents=True, exist_ok=True)

    args = [str(python_path), "-m", "run_bot"]
    if dry_run:
        args.append("--dry-run")
    if file_log:
        args.append("--log")

    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )

    log_handle = console_log_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            args,
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=True,
        )
    finally:
        log_handle.close()

    payload = save_managed_bot_state(
        state_path,
        {
            "pid": int(process.pid),
            "started_at": _utc_now_iso(),
            "started_by": str(requested_by).strip() or "unknown",
            "requested_from": str(requested_from).strip() or "ui",
            "dry_run": bool(dry_run),
            "file_log": bool(file_log),
            "python_path": str(python_path),
            "command": ["-m", "run_bot", *(["--dry-run"] if dry_run else []), *(["--log"] if file_log else [])],
            "console_log_path": str(console_log_path),
        },
    )

    time.sleep(0.5)
    if not is_pid_running(process.pid):
        clear_managed_bot_state(state_path)
        raise RuntimeError(f"Bot process exited immediately. Inspect {console_log_path}")
    return payload


def stop_managed_bot_process(project_root: Path, *, force: bool = True) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    state_path = bot_process_state_path(project_root)
    payload = load_managed_bot_state(state_path)
    if not payload:
        raise RuntimeError("No UI-managed bot process is registered")

    pid = int(payload.get("pid") or 0)
    if pid <= 0:
        clear_managed_bot_state(state_path)
        raise RuntimeError("Managed bot state is missing a valid pid")
    if not is_pid_running(pid):
        clear_managed_bot_state(state_path)
        return {"pid": pid, "stopped": True, "detail": "process_not_running"}

    if os.name == "nt":
        command = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            command.append("/F")
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode not in (0, 128, 255) and is_pid_running(pid):
            # Windows can report "Access denied" while the child tree is already
            # exiting. Give the process a short grace period before treating the
            # taskkill result as authoritative.
            deadline = time.time() + 15
            while time.time() < deadline and is_pid_running(pid):
                time.sleep(0.2)
            if is_pid_running(pid):
                raise RuntimeError((result.stderr or result.stdout or "taskkill failed").strip())
    else:
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 8
        while time.time() < deadline and is_pid_running(pid):
            time.sleep(0.2)
        if is_pid_running(pid) and force:
            os.kill(pid, signal.SIGKILL)

    deadline = time.time() + 4
    while time.time() < deadline and is_pid_running(pid):
        time.sleep(0.2)
    if is_pid_running(pid):
        raise RuntimeError(f"Managed bot pid={pid} did not exit")

    clear_managed_bot_state(state_path)
    return {"pid": pid, "stopped": True, "detail": "terminated"}
