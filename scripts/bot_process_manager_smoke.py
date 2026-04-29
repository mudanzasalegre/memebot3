from __future__ import annotations

import sys
import tempfile
import textwrap
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _check_runtime() -> None:
    print(f"python_executable={sys.executable}")
    if EXPECTED_PYTHON.exists() and Path(sys.executable).resolve() != EXPECTED_PYTHON.resolve():
        raise SystemExit(
            "Bot process manager smoke must be executed with the project venv. "
            f"Use: {EXPECTED_PYTHON} scripts/bot_process_manager_smoke.py"
        )


def main() -> None:
    _check_runtime()

    from runtime.process_manager import (
        bot_process_console_log_path,
        bot_process_state_path,
        is_pid_running,
        load_managed_bot_state,
        start_managed_bot_process,
        stop_managed_bot_process,
    )

    with tempfile.TemporaryDirectory(prefix="memebot3-process-smoke-") as raw_tmp:
        project_root = Path(raw_tmp).resolve()
        (project_root / "data" / "runtime").mkdir(parents=True, exist_ok=True)
        (project_root / "logs").mkdir(parents=True, exist_ok=True)
        (project_root / "run_bot.py").write_text(
            textwrap.dedent(
                """
                import signal
                import time
                from pathlib import Path

                running = True

                def stop(*_args):
                    global running
                    running = False

                for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
                    sig = getattr(signal, name, None)
                    if sig is not None:
                        signal.signal(sig, stop)

                Path("logs/dummy_bot_started.txt").write_text("started", encoding="utf-8")
                while running:
                    time.sleep(0.25)
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

        started = start_managed_bot_process(
            project_root,
            requested_by="bot-process-smoke",
            requested_from="smoke",
            dry_run=True,
            file_log=True,
        )
        pid = int(started["pid"])
        print(f"started_pid={pid}")
        if not is_pid_running(pid):
            raise SystemExit("Started process is not alive")

        state_path = bot_process_state_path(project_root)
        loaded = load_managed_bot_state(state_path)
        if not loaded or int(loaded.get("pid") or 0) != pid:
            raise SystemExit("Managed bot state was not persisted correctly")

        console_log = bot_process_console_log_path(project_root)
        if not console_log.exists():
            raise SystemExit("Managed bot console log was not created")

        stopped = stop_managed_bot_process(project_root, force=True)
        print(f"stopped_pid={stopped.get('pid')}")
        time.sleep(0.5)
        if is_pid_running(pid):
            raise SystemExit("Managed bot process did not stop")
        if load_managed_bot_state(state_path) is not None:
            raise SystemExit("Managed bot state file still exists after stop")

    print("bot_process_manager_smoke=ok")


if __name__ == "__main__":
    main()
