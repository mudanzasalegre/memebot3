from __future__ import annotations

import argparse
import sys
from pathlib import Path

from runtime_backup_lib import create_backup_archive, default_archive_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


def _require_project_python() -> None:
    print(f"python_executable={sys.executable}")
    if EXPECTED_PYTHON.exists() and Path(sys.executable).resolve() != EXPECTED_PYTHON.resolve():
        raise SystemExit(
            "Runtime backup must be executed with the project venv. "
            f"Use: {EXPECTED_PYTHON} scripts/runtime_backup.py"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a basic runtime backup bundle.")
    parser.add_argument("--output", type=Path, default=None, help="Output zip path. Defaults to ./backups/memebot3-backup-<ts>.zip")
    parser.add_argument("--with-env", action="store_true", help="Include .env in the archive")
    parser.add_argument("--with-logs", action="store_true", help="Include the latest log files")
    parser.add_argument("--note", default=None, help="Optional note stored in backup_manifest.json")
    args = parser.parse_args()

    _require_project_python()

    archive_path = (args.output or default_archive_path()).resolve()
    created = create_backup_archive(
        archive_path,
        include_env=bool(args.with_env),
        include_logs=bool(args.with_logs),
        note=args.note,
    )
    print(f"runtime_backup={created}")


if __name__ == "__main__":
    main()
