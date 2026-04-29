from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _check_runtime() -> None:
    print(f"python_executable={sys.executable}")
    print(f"python_version={sys.version.split()[0]}")
    if EXPECTED_PYTHON.exists():
        print(f"expected_python={EXPECTED_PYTHON}")
        if Path(sys.executable).resolve() != EXPECTED_PYTHON.resolve():
            raise SystemExit(
                "Control command smoke must be executed with the project venv. "
                f"Use: {EXPECTED_PYTHON} scripts/control_command_smoke.py"
            )


def _check_imports() -> None:
    for name in ("aiosqlite", "sqlalchemy", "fastapi"):
        mod = importlib.import_module(name)
        print(f"{name}={getattr(mod, '__version__', 'ok')}")


async def _run_smoke() -> None:
    from api.repositories.control_commands import insert_control_command, list_control_commands
    from api.settings import get_settings
    from db.database import async_init_db

    await async_init_db()
    settings = get_settings()

    argv_prev = list(sys.argv)
    try:
        sys.argv = [argv_prev[0]]
        import run_bot
    finally:
        sys.argv = argv_prev

    smoke_bot_id = "smoke"
    root_logger = logging.getLogger()
    original_root_level = run_bot._current_logger_level_name(root_logger)
    target_level = "DEBUG" if original_root_level != "DEBUG" else "INFO"
    original_discovery = bool(run_bot._runtime_discovery_paused)
    original_buys = bool(run_bot._runtime_buys_paused)

    prefix = f"control-smoke-{int(time.time())}"
    inserted: list[tuple[int, str]] = []
    commands = [
        ("pause_discovery", {}, "done"),
        ("pause_discovery", {}, "rejected"),
        ("resume_discovery", {}, "done"),
        ("pause_buys", {}, "done"),
        ("resume_buys", {}, "done"),
        ("set_log_level", {"level": target_level, "logger": "root"}, "done"),
        ("refresh_reports", {"force": False, "include": ["research"]}, "done"),
    ]

    try:
        run_bot._runtime_discovery_paused = False
        run_bot._runtime_buys_paused = False

        for index, (command_type, payload, _expected_status) in enumerate(commands, start=1):
            row, inserted_now = insert_control_command(
                settings.db_path,
                bot_id=smoke_bot_id,
                command_type=command_type,
                payload=payload,
                requested_by="control-command-smoke",
                requested_from="cli",
                idempotency_key=f"{prefix}-{index}",
            )
            if not inserted_now:
                raise SystemExit(f"Unexpected idempotent replay for {command_type}")
            inserted.append((int(row["id"]), _expected_status))

        for _command_id, _expected_status in inserted:
            handled = await run_bot._process_next_control_command(bot_id=smoke_bot_id)
            if not handled:
                raise SystemExit("Expected pending smoke command to be processed")

        rows = list_control_commands(settings.db_path, bot_id=smoke_bot_id, limit=20)
        rows_by_id = {int(row["id"]): row for row in rows}
        for command_id, expected_status in inserted:
            row = rows_by_id.get(command_id)
            if row is None:
                raise SystemExit(f"Smoke command missing from history: {command_id}")
            actual_status = str(row.get("status") or "")
            print(f"command_id={command_id} status={actual_status}")
            if actual_status != expected_status:
                raise SystemExit(f"Unexpected status for command {command_id}: {actual_status} != {expected_status}")

        if run_bot._runtime_discovery_paused:
            raise SystemExit("Discovery pause flag should end false after smoke sequence")
        if run_bot._runtime_buys_paused:
            raise SystemExit("Buys pause flag should end false after smoke sequence")
        if run_bot._current_logger_level_name(root_logger) != target_level:
            raise SystemExit("set_log_level command did not apply the requested root log level")
    finally:
        run_bot._runtime_discovery_paused = original_discovery
        run_bot._runtime_buys_paused = original_buys
        root_logger.setLevel(getattr(logging, original_root_level, logging.INFO))
        try:
            await run_bot._publish_runtime_state_once()
        except Exception:
            pass


def main() -> None:
    _check_runtime()
    _check_imports()
    asyncio.run(_run_smoke())
    print("control_command_smoke=ok")


if __name__ == "__main__":
    main()
