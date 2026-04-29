from __future__ import annotations

import asyncio
import importlib
import json
import sys
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
                "Runtime state smoke must be executed with the project venv. "
                f"Use: {EXPECTED_PYTHON} scripts/runtime_state_smoke.py"
            )


def _check_imports() -> None:
    for name in ("numpy", "pandas", "pyarrow", "aiosqlite", "sqlalchemy"):
        mod = importlib.import_module(name)
        print(f"{name}={getattr(mod, '__version__', 'ok')}")


async def _check_project() -> None:
    from db.database import SessionLocal, async_init_db
    from db.models import BotRuntimeState

    await async_init_db()

    argv_prev = list(sys.argv)
    try:
        sys.argv = [argv_prev[0]]
        import run_bot
    finally:
        sys.argv = argv_prev

    await run_bot._publish_runtime_state_once()

    async with SessionLocal() as session:
        row = await session.get(BotRuntimeState, "main")
        if row is None:
            raise SystemExit("bot_runtime_state row not found after publish")
        print(f"runtime_state.bot_id={row.bot_id}")
        print(f"runtime_state.process_state={row.process_state}")
        print(f"runtime_state.queue_pending={row.queue_pending}")
        print(f"runtime_state.open_positions_count={row.open_positions_count}")
        ml_gate = json.loads(row.ml_gate_json or "{}")
        strategy_health = json.loads(row.strategy_health_json or "{}")
        print(f"runtime_state.ml_gate.mode={ml_gate.get('mode')}")
        print(f"runtime_state.strategy_regimes={','.join(sorted(strategy_health.keys()))}")


def main() -> None:
    _check_runtime()
    _check_imports()
    asyncio.run(_check_project())
    print("runtime_state_smoke=ok")


if __name__ == "__main__":
    main()
