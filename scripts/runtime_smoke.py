from __future__ import annotations

import asyncio
import importlib
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
                "Runtime smoke must be executed with the project venv. "
                f"Use: {EXPECTED_PYTHON} scripts/runtime_smoke.py"
            )


def _check_imports() -> None:
    for name in ("numpy", "pandas", "pyarrow", "aiosqlite", "sqlalchemy"):
        mod = importlib.import_module(name)
        print(f"{name}={getattr(mod, '__version__', 'ok')}")


async def _check_project() -> None:
    from db.database import async_init_db

    await async_init_db()

    argv_prev = list(sys.argv)
    try:
        sys.argv = [argv_prev[0]]
        import run_bot  # noqa: F401
    finally:
        sys.argv = argv_prev


def main() -> None:
    _check_runtime()
    _check_imports()
    asyncio.run(_check_project())
    print("runtime_smoke=ok")


if __name__ == "__main__":
    main()
